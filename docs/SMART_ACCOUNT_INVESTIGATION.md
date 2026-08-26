# Smart Account / DreamAgent Investigation (Phase 0)

**Date:** 2026-08-25  
**Verdict:** **GO** — Delegation Framework is deployed on Somnia Shannon. Runtime is fail-closed; Activate Agent uses live `METAMASK_*` addresses.

## Goal

Confirm MetaMask Hybrid Smart Accounts + ERC-7710 delegation can execute DreamDEX Event Contract trades on Somnia Shannon without per-trade MetaMask popups, while the user remains owner.

## Findings

| # | Question | Result |
| --- | --- | --- |
| 1 | Somnia on official Smart Accounts Kit network list? | **No**. Custom deploy via `deploySmartAccountsEnvironment` / sequential legacy txs. |
| 2 | Hybrid SA without bundler? | **Yes** — Hybrid uses EOA owner; factory deploy is a normal tx. |
| 3 | EOA session key redeem without bundler? | **Yes** — session EOA calls `DelegationManager.redeemDelegations`. |
| 4 | Scope for DreamDEX trades? | **FunctionCall** — pool targets + `placeBinaryOrder`. No transfer / approve / withdraw. |
| 5 | Spend / period / expiry caveats? | Available. App policy still owns traders / copy score. |
| 6 | ERC-7715 Advanced Permissions on Somnia? | **Do not rely on**. DreamLens owns the grant UX. |
| 7 | MetaMask Agent Wallet? | **Out of scope**. |
| 8 | Somnia Agent Kit `AgentVault`? | **Rejected** — agent can withdraw. |
| 9 | DreamDEX `msg.sender` | Copier fills index as the **Smart Account**. |
| 10 | Kit deploy on Shannon | Official helper fails on Somnia RPC (`-32000`). Use the sequential legacy deploy in [`scripts/metamask/spike_deploy_and_trade.mjs`](../scripts/metamask/spike_deploy_and_trade.mjs). |

## Architecture (locked)

```
MetaMask Owner EOA
  → owns Hybrid DreamLens Smart Account
  → signs once: ERC-7710 FunctionCall delegation to DreamAgent session EOA
DreamAgent session EOA (backend)
  → Policy + Risk (off-chain)
  → redeemDelegations → SA executes placeBinaryOrder on DreamDEX
```

AI never signs, never mutates limits, never withdraws.

## Shannon deploy

| Contract | Address |
| --- | --- |
| Deployer (public) | `0x481B210d927765133d55461c3EaCC96F41FdD6C3` |
| SimpleFactory | `0x476643261159d27d2ffe85cafd305c950648b317` |
| DelegationManager | `0xf3f380e58d1742747338c46786cc7d5f9e71ef5c` |
| HybridDeleGatorImpl | `0xed13f0d784b830057d8baa808bb9989bc0e1dd92` |
| EntryPoint | `0x0655b4ba58c1a914ea374f247538a036b50b1ab0` |
| Session EOA (public) | `0xE0588c9a06FB78f15D38785c654cDF6961697c4c` |

Addresses (no keys) are in `scripts/metamask/somnia-environment.json`. Fund the **session EOA** with Shannon STT so DreamAgent can pay gas for `redeemDelegations`.

## Runtime (fail-closed)

- `MOCK_SMART_ACCOUNT=false` (default). Tests force `true` in `tests/conftest.py`.
- Empty `METAMASK_*` → API 503 / Activate Agent hard-error. No invented SA addresses.
- Deposit requires a real Shannon `tx_hash` and a successful receipt.
- Grant requires a MetaMask EIP-712 signature. `0xmock…` is rejected.
- Broadcast requires `DREAM_AGENT_SESSION_KEY`. No SHA256 fake hashes.
- `simulate_copy_alert` requires `DEBUG` + `--force`.

## References

- [Smart Accounts Kit](https://docs.metamask.io/smart-accounts-kit/)
- [Configure / deploy custom environment](https://docs.metamask.io/smart-accounts-kit/guides/configure-toolkit/)
- [Function call scope](https://docs.metamask.io/smart-accounts-kit/guides/delegation/use-delegation-scopes/function-call/)
