/**
 * Deploy MetaMask Delegation Framework on Somnia Shannon.
 *
 * Usage (from this folder):
 *   set -a && source ../../.env && set +a
 *   npm install
 *   SKIP_TRADE=1 node spike_deploy_and_trade.mjs
 *
 * Resumes from somnia-environment.json if present.
 * Never prints private keys. Writes addresses-only JSON + env fragment.
 */

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import {
  createPublicClient,
  createWalletClient,
  defineChain,
  encodeDeployData,
  formatEther,
  http,
} from "viem";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  AllowedCalldataEnforcer,
  AllowedMethodsEnforcer,
  AllowedTargetsEnforcer,
  ApprovalRevocationEnforcer,
  ArgsEqualityCheckEnforcer,
  BlockNumberEnforcer,
  DelegationManager,
  DeployedEnforcer,
  EIP7702StatelessDeleGator,
  ERC1155BalanceChangeEnforcer,
  ERC20BalanceChangeEnforcer,
  ERC20PeriodTransferEnforcer,
  ERC20StreamingEnforcer,
  ERC20TransferAmountEnforcer,
  ERC721BalanceChangeEnforcer,
  ERC721TransferEnforcer,
  EntryPoint,
  ExactCalldataBatchEnforcer,
  ExactCalldataEnforcer,
  ExactExecutionBatchEnforcer,
  ExactExecutionEnforcer,
  HybridDeleGator,
  IdEnforcer,
  LimitedCallsEnforcer,
  MultiSigDeleGator,
  MultiTokenPeriodEnforcer,
  NativeBalanceChangeEnforcer,
  NativeTokenPaymentEnforcer,
  NativeTokenPeriodTransferEnforcer,
  NativeTokenStreamingEnforcer,
  NativeTokenTransferAmountEnforcer,
  NonceEnforcer,
  OwnershipTransferEnforcer,
  RedeemerEnforcer,
  SCL_RIP7212,
  SimpleFactory,
  SpecificActionERC20TransferBatchEnforcer,
  TimestampEnforcer,
  ValueLteEnforcer,
} from "@metamask/delegation-abis";
import {
  AllowedCalldataEnforcer as AllowedCalldataEnforcerBytecode,
  AllowedMethodsEnforcer as AllowedMethodsEnforcerBytecode,
  AllowedTargetsEnforcer as AllowedTargetsEnforcerBytecode,
  ApprovalRevocationEnforcer as ApprovalRevocationEnforcerBytecode,
  ArgsEqualityCheckEnforcer as ArgsEqualityCheckEnforcerBytecode,
  BlockNumberEnforcer as BlockNumberEnforcerBytecode,
  DelegationManager as DelegationManagerBytecode,
  DeployedEnforcer as DeployedEnforcerBytecode,
  EIP7702StatelessDeleGator as EIP7702StatelessDeleGatorBytecode,
  ERC1155BalanceChangeEnforcer as ERC1155BalanceChangeEnforcerBytecode,
  ERC20BalanceChangeEnforcer as ERC20BalanceChangeEnforcerBytecode,
  ERC20PeriodTransferEnforcer as ERC20PeriodTransferEnforcerBytecode,
  ERC20StreamingEnforcer as ERC20StreamingEnforcerBytecode,
  ERC20TransferAmountEnforcer as ERC20TransferAmountEnforcerBytecode,
  ERC721BalanceChangeEnforcer as ERC721BalanceChangeEnforcerBytecode,
  ERC721TransferEnforcer as ERC721TransferEnforcerBytecode,
  EntryPoint as EntryPointBytecode,
  ExactCalldataBatchEnforcer as ExactCalldataBatchEnforcerBytecode,
  ExactCalldataEnforcer as ExactCalldataEnforcerBytecode,
  ExactExecutionBatchEnforcer as ExactExecutionBatchEnforcerBytecode,
  ExactExecutionEnforcer as ExactExecutionEnforcerBytecode,
  HybridDeleGator as HybridDeleGatorBytecode,
  IdEnforcer as IdEnforcerBytecode,
  LimitedCallsEnforcer as LimitedCallsEnforcerBytecode,
  MultiSigDeleGator as MultiSigDeleGatorBytecode,
  MultiTokenPeriodEnforcer as MultiTokenPeriodEnforcerBytecode,
  NativeBalanceChangeEnforcer as NativeBalanceChangeEnforcerBytecode,
  NativeTokenPaymentEnforcer as NativeTokenPaymentEnforcerBytecode,
  NativeTokenPeriodTransferEnforcer as NativeTokenPeriodTransferEnforcerBytecode,
  NativeTokenStreamingEnforcer as NativeTokenStreamingEnforcerBytecode,
  NativeTokenTransferAmountEnforcer as NativeTokenTransferAmountEnforcerBytecode,
  NonceEnforcer as NonceEnforcerBytecode,
  OwnershipTransferEnforcer as OwnershipTransferEnforcerBytecode,
  RedeemerEnforcer as RedeemerEnforcerBytecode,
  SCL_RIP7212 as SCLRIP7212Bytecode,
  SimpleFactory as SimpleFactoryBytecode,
  SpecificActionERC20TransferBatchEnforcer as SpecificActionERC20TransferBatchEnforcerBytecode,
  TimestampEnforcer as TimestampEnforcerBytecode,
  ValueLteEnforcer as ValueLteEnforcerBytecode,
} from "@metamask/delegation-abis/bytecode";

const chainId = Number(process.env.CHAIN_ID || process.env.DREAMDEX_CHAIN_ID || 50312);
const rpcUrl =
  process.env.RPC_URL ||
  process.env.DREAMDEX_RPC_URL ||
  "https://dream-rpc.somnia.network";

const somniaShannon = defineChain({
  id: chainId,
  name: "Somnia Shannon",
  nativeCurrency: { name: "STT", symbol: "STT", decimals: 18 },
  rpcUrls: { default: { http: [rpcUrl] } },
});

const envJsonUrl = new URL("./somnia-environment.json", import.meta.url);
const envFragmentUrl = new URL("./env.fragment", import.meta.url);

function normalizeKey(raw) {
  const key = String(raw || "").trim();
  if (!key) return "";
  return key.startsWith("0x") ? key : `0x${key}`;
}

function hexBytecode(value) {
  if (typeof value === "string") return value;
  if (value && typeof value.object === "string") return value.object;
  if (value && typeof value.bytecode === "string") return value.bytecode;
  throw new Error(`Unexpected bytecode shape: ${typeof value}`);
}

function loadProgress() {
  const path = fileURLToPath(envJsonUrl);
  if (!existsSync(path)) return {};
  try {
    const parsed = JSON.parse(readFileSync(path, "utf8"));
    const merged = {
      ...(parsed.deployedContracts || {}),
      DelegationManager: parsed.environment?.DelegationManager,
      SimpleFactory: parsed.environment?.SimpleFactory,
      HybridDeleGatorImpl:
        parsed.environment?.HybridDeleGator ||
        parsed.environment?.implementations?.HybridDeleGatorImpl,
      EntryPoint: parsed.environment?.EntryPoint,
    };
    return Object.fromEntries(
      Object.entries(merged).filter(([, value]) => Boolean(value)),
    );
  } catch {
    return {};
  }
}

function persist(out, sessionKey) {
  writeFileSync(envJsonUrl, JSON.stringify(out, null, 2));
  const env = out.environment || {};
  const fragment = [
    "MOCK_SMART_ACCOUNT=false",
    `METAMASK_DELEGATION_MANAGER=${env.DelegationManager || ""}`,
    `METAMASK_SIMPLE_FACTORY=${env.SimpleFactory || ""}`,
    `METAMASK_HYBRID_IMPL=${env.HybridDeleGator || ""}`,
    `METAMASK_ENTRY_POINT=${env.EntryPoint || ""}`,
    `DREAM_AGENT_SESSION_KEY=${sessionKey}`,
    "",
  ].join("\n");
  writeFileSync(envFragmentUrl, fragment);
}

function rpcDetail(err) {
  return [
    err?.shortMessage,
    err?.details,
    err?.cause?.details,
    err?.cause?.shortMessage,
    err?.message,
  ]
    .filter(Boolean)
    .join(" | ");
}

async function deployOne({
  name,
  abi,
  bytecode,
  args = [],
  walletClient,
  publicClient,
  account,
  deployed,
}) {
  if (deployed[name]) {
    console.log("Skip", name, deployed[name]);
    return deployed[name];
  }
  const data = encodeDeployData({
    abi,
    bytecode: hexBytecode(bytecode),
    args,
  });
  const gasPrice = await publicClient.getGasPrice();
  const balance = await publicClient.getBalance({ address: account.address });
  const byteLen = BigInt((data.length - 2) / 2);
  // Unused gas is refunded. Prefer a high limit — Somnia reverts if the cap is tight.
  let gas = 1_200_000n + byteLen * 400n;
  try {
    const estimated = await publicClient.estimateGas({
      account: account.address,
      data,
    });
    if (estimated > 0n) {
      const padded = (estimated * 120n) / 100n;
      if (padded > gas) gas = padded;
    }
  } catch (err) {
    console.warn("estimateGas failed for", name, rpcDetail(err));
  }
  const affordable = balance > 0n ? (balance * 85n) / (gasPrice * 100n) : 0n;
  if (byteLen > 10_000n && gas < 50_000_000n) {
    gas = 50_000_000n;
  }
  if (affordable > 21_000n && gas > affordable) {
    gas = affordable;
  }
  const cost = gas * gasPrice;
  if (cost > balance) {
    throw new Error(
      `${name}: need ~${formatEther(cost)} STT, have ${formatEther(balance)} STT. ` +
        "Fund the deployer at https://testnet.somnia.network/ and re-run (progress is saved).",
    );
  }
  console.log(
    "Deploy",
    name,
    "gas",
    gas.toString(),
    "gasPrice",
    gasPrice.toString(),
    "cost~",
    formatEther(cost),
    "STT",
  );
  const hash = await walletClient.sendTransaction({
    account,
    chain: somniaShannon,
    data,
    gas,
    gasPrice,
    type: "legacy",
  });
  console.log("  tx", hash);
  const receipt = await publicClient.waitForTransactionReceipt({ hash });
  if (receipt.status !== "success" || !receipt.contractAddress) {
    throw new Error(`${name} deploy failed status=${receipt.status}`);
  }
  deployed[name] = receipt.contractAddress;
  console.log("  →", receipt.contractAddress, "gasUsed", receipt.gasUsed?.toString?.() || receipt.gasUsed);
  return receipt.contractAddress;
}

async function main() {
  const deployerKey = normalizeKey(process.env.DEPLOYER_PRIVATE_KEY);
  if (!deployerKey) {
    console.error("Set DEPLOYER_PRIVATE_KEY to deploy the Delegation Framework.");
    process.exit(1);
  }

  const account = privateKeyToAccount(/** @type {`0x${string}`} */ (deployerKey));
  const publicClient = createPublicClient({
    chain: somniaShannon,
    transport: http(rpcUrl),
  });
  const walletClient = createWalletClient({
    account,
    chain: somniaShannon,
    transport: http(rpcUrl),
  });

  const balance = await publicClient.getBalance({ address: account.address });
  console.log("Chain", chainId);
  console.log("RPC", rpcUrl);
  console.log("Deployer", account.address);
  console.log("Balance", formatEther(balance), "STT");

  if (balance === 0n) {
    console.error(
      "Deployer has 0 STT. Fund it on Shannon (https://testnet.somnia.network/) then re-run.",
    );
    process.exit(2);
  }

  let sessionKey = normalizeKey(process.env.DREAM_AGENT_SESSION_KEY);
  let sessionGenerated = false;
  if (!sessionKey) {
    sessionKey = generatePrivateKey();
    sessionGenerated = true;
  }
  const sessionAccount = privateKeyToAccount(/** @type {`0x${string}`} */ (sessionKey));
  console.log("Session EOA", sessionAccount.address);
  if (sessionGenerated) {
    console.log("Generated DREAM_AGENT_SESSION_KEY (written to env.fragment only; do not commit).");
  }

  const deployed = loadProgress();
  const writeSnapshot = () => {
    persist(
      {
        chainId,
        rpcUrl,
        deployedAt: new Date().toISOString(),
        deployer: account.address,
        sessionAddress: sessionAccount.address,
        deployedContracts: deployed,
        environment: {
          DelegationManager: deployed.DelegationManager || "",
          SimpleFactory: deployed.SimpleFactory || "",
          HybridDeleGator: deployed.HybridDeleGatorImpl || "",
          EntryPoint: deployed.EntryPoint || "",
          implementations: {
            HybridDeleGatorImpl: deployed.HybridDeleGatorImpl || "",
            MultiSigDeleGatorImpl: deployed.MultiSigDeleGatorImpl || "",
            EIP7702StatelessDeleGatorImpl: deployed.EIP7702StatelessDeleGatorImpl || "",
          },
        },
      },
      sessionKey,
    );
  };

  const ctx = { walletClient, publicClient, account, deployed };
  const standalone = [
    ["SimpleFactory", SimpleFactory, SimpleFactoryBytecode],
    ["AllowedCalldataEnforcer", AllowedCalldataEnforcer, AllowedCalldataEnforcerBytecode],
    ["AllowedTargetsEnforcer", AllowedTargetsEnforcer, AllowedTargetsEnforcerBytecode],
    ["AllowedMethodsEnforcer", AllowedMethodsEnforcer, AllowedMethodsEnforcerBytecode],
    ["ApprovalRevocationEnforcer", ApprovalRevocationEnforcer, ApprovalRevocationEnforcerBytecode],
    ["ArgsEqualityCheckEnforcer", ArgsEqualityCheckEnforcer, ArgsEqualityCheckEnforcerBytecode],
    ["DeployedEnforcer", DeployedEnforcer, DeployedEnforcerBytecode],
    ["TimestampEnforcer", TimestampEnforcer, TimestampEnforcerBytecode],
    ["BlockNumberEnforcer", BlockNumberEnforcer, BlockNumberEnforcerBytecode],
    ["LimitedCallsEnforcer", LimitedCallsEnforcer, LimitedCallsEnforcerBytecode],
    ["ERC20BalanceChangeEnforcer", ERC20BalanceChangeEnforcer, ERC20BalanceChangeEnforcerBytecode],
    ["ERC20TransferAmountEnforcer", ERC20TransferAmountEnforcer, ERC20TransferAmountEnforcerBytecode],
    ["ERC20StreamingEnforcer", ERC20StreamingEnforcer, ERC20StreamingEnforcerBytecode],
    ["ERC721BalanceChangeEnforcer", ERC721BalanceChangeEnforcer, ERC721BalanceChangeEnforcerBytecode],
    ["ERC721TransferEnforcer", ERC721TransferEnforcer, ERC721TransferEnforcerBytecode],
    ["ERC1155BalanceChangeEnforcer", ERC1155BalanceChangeEnforcer, ERC1155BalanceChangeEnforcerBytecode],
    ["IdEnforcer", IdEnforcer, IdEnforcerBytecode],
    ["NonceEnforcer", NonceEnforcer, NonceEnforcerBytecode],
    ["ValueLteEnforcer", ValueLteEnforcer, ValueLteEnforcerBytecode],
    ["NativeTokenTransferAmountEnforcer", NativeTokenTransferAmountEnforcer, NativeTokenTransferAmountEnforcerBytecode],
    ["NativeBalanceChangeEnforcer", NativeBalanceChangeEnforcer, NativeBalanceChangeEnforcerBytecode],
    ["NativeTokenStreamingEnforcer", NativeTokenStreamingEnforcer, NativeTokenStreamingEnforcerBytecode],
    ["OwnershipTransferEnforcer", OwnershipTransferEnforcer, OwnershipTransferEnforcerBytecode],
    ["RedeemerEnforcer", RedeemerEnforcer, RedeemerEnforcerBytecode],
    ["SpecificActionERC20TransferBatchEnforcer", SpecificActionERC20TransferBatchEnforcer, SpecificActionERC20TransferBatchEnforcerBytecode],
    ["ERC20PeriodTransferEnforcer", ERC20PeriodTransferEnforcer, ERC20PeriodTransferEnforcerBytecode],
    ["NativeTokenPeriodTransferEnforcer", NativeTokenPeriodTransferEnforcer, NativeTokenPeriodTransferEnforcerBytecode],
    ["ExactCalldataBatchEnforcer", ExactCalldataBatchEnforcer, ExactCalldataBatchEnforcerBytecode],
    ["ExactCalldataEnforcer", ExactCalldataEnforcer, ExactCalldataEnforcerBytecode],
    ["ExactExecutionEnforcer", ExactExecutionEnforcer, ExactExecutionEnforcerBytecode],
    ["ExactExecutionBatchEnforcer", ExactExecutionBatchEnforcer, ExactExecutionBatchEnforcerBytecode],
    ["MultiTokenPeriodEnforcer", MultiTokenPeriodEnforcer, MultiTokenPeriodEnforcerBytecode],
  ];

  const essentialOnly = process.env.MINIMAL_FRAMEWORK === "1";
  const essential = new Set([
    "SimpleFactory",
    "AllowedTargetsEnforcer",
    "AllowedMethodsEnforcer",
    "TimestampEnforcer",
    "ERC20PeriodTransferEnforcer",
    "AllowedCalldataEnforcer",
    "ValueLteEnforcer",
    "NonceEnforcer",
  ]);

  for (const [name, abi, bytecode] of standalone) {
    if (essentialOnly && !essential.has(name)) continue;
    await deployOne({ name, abi, bytecode, ...ctx });
    writeSnapshot();
  }

  await deployOne({
    name: "DelegationManager",
    abi: DelegationManager,
    bytecode: DelegationManagerBytecode,
    args: [account.address],
    ...ctx,
  });
  writeSnapshot();

  if (!essentialOnly) {
    await deployOne({
      name: "NativeTokenPaymentEnforcer",
      abi: NativeTokenPaymentEnforcer,
      bytecode: NativeTokenPaymentEnforcerBytecode,
      args: [deployed.DelegationManager, deployed.ArgsEqualityCheckEnforcer],
      ...ctx,
    });
    writeSnapshot();
  }

  await deployOne({
    name: "EntryPoint",
    abi: EntryPoint,
    bytecode: EntryPointBytecode,
    ...ctx,
  });
  writeSnapshot();

  const sclRIP7212 = await deployOne({
    name: "SCL_RIP7212",
    abi: SCL_RIP7212,
    bytecode: SCLRIP7212Bytecode,
    ...ctx,
  });
  writeSnapshot();

  const hybridBytecode = hexBytecode(HybridDeleGatorBytecode).replace(
    /__\$b8f96b288d4d0429e38b8ed50fd423070f\$__/gu,
    sclRIP7212.slice(2),
  );
  await deployOne({
    name: "HybridDeleGatorImpl",
    abi: HybridDeleGator,
    bytecode: hybridBytecode,
    args: [deployed.DelegationManager, deployed.EntryPoint],
    ...ctx,
  });
  writeSnapshot();

  if (!essentialOnly) {
    await deployOne({
      name: "MultiSigDeleGatorImpl",
      abi: MultiSigDeleGator,
      bytecode: MultiSigDeleGatorBytecode,
      args: [deployed.DelegationManager, deployed.EntryPoint],
      ...ctx,
    });
    await deployOne({
      name: "EIP7702StatelessDeleGatorImpl",
      abi: EIP7702StatelessDeleGator,
      bytecode: EIP7702StatelessDeleGatorBytecode,
      args: [deployed.DelegationManager, deployed.EntryPoint],
      ...ctx,
    });
    writeSnapshot();
  }

  writeSnapshot();
  console.log("Wrote scripts/metamask/somnia-environment.json (addresses only)");
  console.log("Wrote scripts/metamask/env.fragment (includes session key — gitignored)");
  console.log("DelegationManager", deployed.DelegationManager);
  console.log("SimpleFactory", deployed.SimpleFactory);
  console.log("Hybrid", deployed.HybridDeleGatorImpl);
  console.log("EntryPoint", deployed.EntryPoint);

  if (process.env.SKIP_TRADE === "1") {
    console.log("SKIP_TRADE=1 — deploy only.");
    return;
  }
  console.log("Framework deployed. Fund the session EOA with STT for gas, then grant + redeem from DreamLens.");
}

main().catch((err) => {
  console.error(rpcDetail(err) || err);
  if (err?.cause) console.error(err.cause);
  process.exit(1);
});
