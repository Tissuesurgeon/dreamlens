"""Smart Account pays DreamAgent Shannon gas via redeem reimbursement."""

from __future__ import annotations

from eth_utils import function_signature_to_4byte_selector
from integrations.metamask import EXECUTION_MODE_BATCH_DEFAULT
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


def test_encode_redeem_batch_includes_gas_payment():
    single = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
    )
    batch = encode_redeem_delegations_calldata(
        signed_delegation=DELEGATION,
        target=POOL,
        call_data="0xabcdef",
        extra_executions=[(SESSION, 10**15, "0x")],
    )
    assert single.startswith("0x")
    assert batch.startswith("0x")
    selector = function_signature_to_4byte_selector(REDEEM_DELEGATIONS_SIGNATURE).hex()
    assert single[2:].startswith(selector)
    assert batch[2:].startswith(selector)
    assert len(batch) > len(single)
    assert EXECUTION_MODE_BATCH_DEFAULT[2:] in batch[2:]
    assert SESSION[2:].lower() in batch.lower()
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
    assert EXECUTION_MODE_BATCH_DEFAULT[2:] in updated.data[2:]


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


def test_agent_can_includes_paying_gas():
    assert any("gas" in item.lower() for item in DreamAgentPermissionSpec().agent_can())
