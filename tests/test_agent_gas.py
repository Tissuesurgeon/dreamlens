"""Smart Account pays DreamAgent Shannon gas via redeem reimbursement."""

from __future__ import annotations

from eth_utils import function_signature_to_4byte_selector
from integrations.metamask import EXECUTION_MODE_BATCH_DEFAULT, EXECUTION_MODE_SINGLE_DEFAULT
from integrations.metamask.delegation import (
    REDEEM_DELEGATIONS_SIGNATURE,
    encode_redeem_delegations_calldata,
)
from integrations.metamask.execution import (
    DelegatedExecution,
    compute_gas_reimbursement_wei,
    with_gas_reimbursement,
)
from integrations.metamask.permissions import DreamAgentPermissionSpec
from integrations.metamask.transactions import apply_smart_account_gas_payment

SA = "0x481B210d927765133d55461c3EaCC96F41FdD6C3"
SESSION = "0xE0588c9a06FB78f15D38785c654cDF6961697c4c"
POOL = "0x" + "22" * 20
SIG = "0x" + "ab" * 65

DELEGATION = {
    "delegate": SESSION,
    "delegator": SA,
    "authority": "0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
    "caveats": [],
    "salt": "0x1",
    "signature": SIG,
}


def _execution(**kwargs) -> DelegatedExecution:
    defaults = dict(
        to="0xf3f380e58d1742747338c46786cc7d5f9e71ef5c",
        data="0x1234",
        value=0,
        chain_id=50312,
        mock=False,
        inner_target=POOL,
        inner_data="0xabcdef",
        inner_value=0,
        signed_delegation=DELEGATION,
    )
    defaults.update(kwargs)
    return DelegatedExecution(**defaults)


def test_compute_gas_reimbursement_zero_when_sa_empty():
    assert (
        compute_gas_reimbursement_wei(
            sa_balance_wei=0,
            gas_limit=800_000,
            gas_price_wei=1_000_000_000,
        )
        == 0
    )


def test_compute_gas_reimbursement_applies_buffer_and_cap():
    # 800_000 * 1 gwei * 1.25 = 0.001 STT
    assert (
        compute_gas_reimbursement_wei(
            sa_balance_wei=10**18,
            gas_limit=800_000,
            gas_price_wei=1_000_000_000,
            buffer_bps=2500,
            cap_wei=5 * 10**16,
        )
        == 10**15
    )
    assert (
        compute_gas_reimbursement_wei(
            sa_balance_wei=10**14,
            gas_limit=800_000,
            gas_price_wei=1_000_000_000,
        )
        == 10**14
    )
    assert (
        compute_gas_reimbursement_wei(
            sa_balance_wei=10**18,
            gas_limit=800_000,
            gas_price_wei=1_000_000_000,
            cap_wei=10**12,
        )
        == 10**12
    )


def test_encode_redeem_multi_uses_single_default_modes():
    single = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
    )
    multi = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
        pre_executions=[("0x" + "33" * 20, 0, "0x095ea7b3")],
    )
    assert single.startswith("0x")
    assert multi.startswith("0x")
    selector = function_signature_to_4byte_selector(REDEEM_DELEGATIONS_SIGNATURE).hex()
    assert single[2:].startswith(selector)
    assert multi[2:].startswith(selector)
    assert len(multi) > len(single)
    assert EXECUTION_MODE_BATCH_DEFAULT not in multi
    assert EXECUTION_MODE_SINGLE_DEFAULT[2:] in multi[2:]
    packed_pool = POOL[2:].lower()
    assert packed_pool in single.lower()


def test_with_gas_reimbursement_rewrites_calldata():
    original = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
    )
    execution = _execution(data=original)
    updated = with_gas_reimbursement(
        execution, recipient=SESSION, amount_wei=10**15
    )
    assert updated.gas_payment_wei == 10**15
    assert updated.data != original
    assert EXECUTION_MODE_BATCH_DEFAULT not in updated.data


def test_apply_smart_account_gas_payment_when_sa_has_stt(settings):
    settings.DREAM_AGENT_SA_PAYS_GAS = True
    settings.DREAM_AGENT_GAS_LIMIT = 800_000
    settings.DREAM_AGENT_GAS_BUFFER_BPS = 2500
    settings.DREAM_AGENT_MAX_GAS_PAYMENT_WEI = 5 * 10**16

    class FakeEth:
        gas_price = 1_000_000_000

        def get_balance(self, _addr):
            return 10**18

    class FakeW3:
        eth = FakeEth()

    original = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
    )
    updated = apply_smart_account_gas_payment(
        _execution(data=original),
        session_address=SESSION,
        w3=FakeW3(),
    )
    assert updated.gas_payment_wei == 10**15
    assert updated.data != original


def test_apply_smart_account_gas_payment_skips_function_call_caveats(settings):
    settings.DREAM_AGENT_SA_PAYS_GAS = True

    class FakeEth:
        gas_price = 1_000_000_000

        def get_balance(self, _addr):
            return 10**18

    class FakeW3:
        eth = FakeEth()

    original = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
    )
    caveated = dict(DELEGATION)
    caveated["caveats"] = [
        {"enforcer": "0x" + "cc" * 20, "terms": "0xabcdef12", "args": "0x"}
    ]
    updated = apply_smart_account_gas_payment(
        _execution(data=original, signed_delegation=caveated),
        session_address=SESSION,
        w3=FakeW3(),
    )
    assert updated.gas_payment_wei == 0
    assert updated.data == original


def test_apply_smart_account_gas_payment_skips_empty_sa(settings):
    settings.DREAM_AGENT_SA_PAYS_GAS = True

    class FakeEth:
        gas_price = 1_000_000_000

        def get_balance(self, _addr):
            return 0

    class FakeW3:
        eth = FakeEth()

    original = "0x1234"
    updated = apply_smart_account_gas_payment(
        _execution(data=original),
        session_address=SESSION,
        w3=FakeW3(),
    )
    assert updated.gas_payment_wei == 0
    assert updated.data == original


def test_agent_can_includes_one_signature_trades():
    can = DreamAgentPermissionSpec().agent_can()
    assert any("signature" in item.lower() for item in can)
    assert any("claim" in item.lower() for item in can)
    assert all("automatically" not in item.lower() for item in can)


def test_legacy_session_tx_is_not_eip1559():
    from eth_account import Account

    from integrations.metamask.transactions import legacy_session_tx

    acct = Account.from_key("0x" + "11" * 32)
    tx = legacy_session_tx(
        to=POOL,
        data="0xabcdef",
        value=0,
        chain_id=50312,
        nonce=0,
        gas=800_000,
        gas_price=1_000_000_000,
    )
    assert "gasPrice" in tx
    assert "maxFeePerGas" not in tx
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    assert raw[:1] != b"\x02"


def test_humanize_somnia_type2_reject():
    from integrations.metamask.transactions import _humanize_redeem_revert

    msg = _humanize_redeem_revert(
        Exception("{'code': -32000, 'message': 'account does not exist', 'data': '0x02'}")
    )
    assert "legacy" in msg.lower() or "session" in msg.lower()


def test_wrap_owner_execute_targets_smart_account():
    from integrations.dreamdex.types import UnsignedTxDTO
    from integrations.metamask.execution import wrap_owner_execute

    inner = UnsignedTxDTO(
        to=POOL,
        data="0xabcdef",
        value=0,
        chain_id=50312,
        description="redeem",
    )
    wrapped = wrap_owner_execute(SA, inner)
    assert wrapped.to.lower() == SA.lower()
    assert wrapped.data.startswith("0x")
    from eth_utils import function_signature_to_4byte_selector

    selector = "0x" + function_signature_to_4byte_selector("execute(bytes32,bytes)").hex()
    assert wrapped.data.lower().startswith(selector.lower())
    assert wrapped.metadata["inner_to"].lower() == POOL.lower()
