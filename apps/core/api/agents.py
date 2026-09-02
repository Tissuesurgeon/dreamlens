"""DreamAgent / Smart Account REST API."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.agents.models import AgentEvaluation, DreamAgent
from apps.dreamcopy.models import CopyRelationship
from integrations.metamask.smart_account import SmartAccountConfigError
from services import dream_agent_service, smart_account_service
from services.dream_agent_service import DreamAgentError
from services.smart_account_service import SmartAccountError


def _dec(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


class SmartAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        sa = smart_account_service.get_account(request.user)
        if not sa:
            return Response({"smart_account": None, "grant": smart_account_service.grant_payload_for_ui(request.user)})
        try:
            bal = smart_account_service.get_balance(sa)
        except SmartAccountError as exc:
            bal = {"error": str(exc), "address": sa.address}
        agent = (
            DreamAgent.objects.filter(user=request.user, smart_account=sa)
            .exclude(status=DreamAgent.Status.REVOKED)
            .order_by("-updated_at")
            .first()
        )
        return Response(
            {
                "smart_account": {
                    "id": sa.pk,
                    "address": sa.address,
                    "owner_address": sa.owner_address,
                    "status": sa.status,
                    "chain_id": sa.chain_id,
                },
                "balance": bal,
                "agent": (
                    {
                        "id": agent.pk,
                        "name": agent.name,
                        "status": agent.status,
                        "session_address": agent.session_address,
                    }
                    if agent
                    else None
                ),
                "grant": smart_account_service.grant_payload_for_ui(request.user),
            }
        )

    def post(self, request):
        owner = request.data.get("owner_address") or request.data.get("wallet_address")
        if not owner:
            return Response(
                {"detail": "owner_address required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            sa = smart_account_service.create_account(
                request.user,
                owner_address=owner,
                address=request.data.get("address"),
                factory_address=request.data.get("factory_address") or "",
                deploy_salt=request.data.get("deploy_salt") or "0x",
                metadata=request.data.get("metadata") or {},
            )
            smart_account_service.ensure_agent(request.user, sa)
        except SmartAccountConfigError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "smart_account": {
                    "id": sa.pk,
                    "address": sa.address,
                    "owner_address": sa.owner_address,
                    "status": sa.status,
                    "chain_id": sa.chain_id,
                }
            },
            status=status.HTTP_201_CREATED,
        )


class SmartAccountDepositView(APIView):
    """Record a deposit intent / confirmation (user sends funds from MetaMask)."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        sa = smart_account_service.get_account(request.user)
        if not sa:
            return Response(
                {"detail": "Create a Smart Account first"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        amount = _dec(request.data.get("amount"), "0")
        tx_hash = request.data.get("tx_hash") or ""
        try:
            proof = smart_account_service.verify_deposit_tx(
                tx_hash=tx_hash, smart_account=sa
            )
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        meta = dict(sa.metadata_json or {})
        if amount > 0:
            meta["last_deposit"] = str(amount)
        meta["last_deposit_tx"] = proof["tx_hash"]
        sa.metadata_json = meta
        sa.save(update_fields=["metadata_json", "updated_at"])
        smart_account_service.mark_funded(sa, amount=amount if amount > 0 else None)
        try:
            bal = smart_account_service.get_balance(sa)
        except SmartAccountError:
            bal = {"tx_hash": proof["tx_hash"]}
        return Response(
            {"smart_account": {"id": sa.pk, "status": sa.status}, "balance": bal}
        )


class SmartAccountWithdrawView(APIView):
    """Owner withdraws SA USDC to the MetaMask that owns the account."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        amount = request.query_params.get("amount")
        try:
            payload = smart_account_service.prepare_owner_withdraw(
                request.user,
                amount,
            )
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)

    def post(self, request):
        amount = request.data.get("amount")
        tx_hash = request.data.get("tx_hash") or ""
        signature = request.data.get("signature") or ""
        if tx_hash:
            try:
                payload = smart_account_service.confirm_owner_withdraw(
                    request.user,
                    tx_hash=tx_hash,
                    amount=amount,
                )
            except SmartAccountError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            return Response(payload)
        if not signature:
            return Response(
                {"detail": "signature or tx_hash required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            payload = smart_account_service.prepare_owner_withdraw(
                request.user,
                amount,
                signature=signature,
                salt=request.data.get("salt"),
                expires_at=request.data.get("expires_at"),
            )
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(payload)


class DreamAgentGrantView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        owner = request.query_params.get("owner") or request.query_params.get("owner_address") or ""
        return Response(
            smart_account_service.grant_payload_for_ui(request.user, owner_address=owner)
        )

    def post(self, request):
        data = request.data
        allowed_traders = data.get("allowed_traders")
        if not allowed_traders:
            # Default to all active copy relationships
            allowed_traders = list(
                CopyRelationship.objects.filter(
                    user=request.user,
                    status=CopyRelationship.Status.ACTIVE,
                ).values_list("trader_id", flat=True)
            )
        try:
            agent, perm = smart_account_service.grant_agent(
                request.user,
                max_trade_amount=_dec(data.get("max_trade_amount"), "10"),
                max_daily_volume=_dec(data.get("max_daily_volume"), "50"),
                expires_in_days=int(data.get("expires_in_days") or 30),
                min_copy_score=int(data.get("min_copy_score") or 75),
                allowed_traders=allowed_traders,
                allowed_outcomes=data.get("allowed_outcomes") or [],
                allowed_contracts=data.get("allowed_contracts") or [],
                signed_delegation=data.get("signed_delegation"),
                activate=bool(data.get("activate", True)),
            )
        except SmartAccountConfigError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Mirror limits onto copy relationships
        CopyRelationship.objects.filter(
            user=request.user,
            status=CopyRelationship.Status.ACTIVE,
        ).update(
            max_per_trade=perm.max_trade_amount,
            max_daily=perm.max_daily_volume,
            min_copy_score=perm.min_copy_score,
        )

        return Response(
            {
                "agent": {
                    "id": agent.pk,
                    "name": agent.name,
                    "status": agent.status,
                    "session_address": agent.session_address,
                },
                "permission": {
                    "id": perm.pk,
                    "status": perm.status,
                    "max_trade_amount": str(perm.max_trade_amount),
                    "max_daily_volume": str(perm.max_daily_volume),
                    "min_copy_score": perm.min_copy_score,
                    "expires_at": perm.expires_at.isoformat(),
                },
            },
            status=status.HTTP_201_CREATED,
        )


class DreamAgentStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        agent = (
            DreamAgent.objects.filter(user=request.user)
            .exclude(status=DreamAgent.Status.REVOKED)
            .select_related("smart_account")
            .order_by("-updated_at")
            .first()
        )
        if not agent:
            revoked = (
                DreamAgent.objects.filter(
                    user=request.user, status=DreamAgent.Status.REVOKED
                )
                .select_related("smart_account")
                .order_by("-updated_at")
                .first()
            )
            if revoked:
                return Response(dream_agent_service.agent_performance(revoked))
            return Response({"agent": None})
        return Response(dream_agent_service.agent_performance(agent))

    def patch(self, request):
        new_status = request.data.get("status")
        if not new_status:
            return Response(
                {"detail": "status required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            agent = smart_account_service.set_agent_status(
                request.user,
                new_status,
                agent_id=request.data.get("agent_id"),
            )
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(dream_agent_service.agent_performance(agent))


class DreamAgentRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            agent = smart_account_service.revoke_agent(
                request.user,
                agent_id=request.data.get("agent_id"),
            )
        except SmartAccountError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {
                "agent": {"id": agent.pk, "status": agent.status},
                "detail": "DreamAgent revoked. Your Smart Account remains yours.",
            }
        )


class DreamAgentEvaluationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        agent = (
            DreamAgent.objects.filter(user=request.user)
            .order_by("-updated_at")
            .first()
        )
        if not agent:
            return Response({"results": []})
        decision = request.query_params.get("decision")
        qs = AgentEvaluation.objects.filter(agent=agent)
        if decision:
            qs = qs.filter(decision=decision.upper())
        skipped_only = request.query_params.get("skipped")
        if skipped_only in ("1", "true", "yes"):
            qs = qs.filter(decision=AgentEvaluation.Decision.SKIPPED)
        results = [
            dream_agent_service.serialize_evaluation(ev)
            for ev in qs[:50]
        ]
        return Response({"results": results, "agent_id": agent.pk})


class AgentTradeView(APIView):
    """Web Buy YES/NO through the session key — same path as Telegram /trade."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.core.api.serializers import TradeSerializer

        raw_id = request.data.get("event_id") if isinstance(request.data, dict) else None
        outcome = (request.data.get("outcome") if isinstance(request.data, dict) else None) or ""
        try:
            event_id = int(raw_id)
        except (TypeError, ValueError):
            return Response(
                {"detail": "event_id must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        amount = _dec(request.data.get("amount") if isinstance(request.data, dict) else None, "0")
        if amount <= 0:
            return Response(
                {"detail": "Amount must be positive."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            trade = dream_agent_service.execute_agent_manual_trade(
                request.user,
                event_id=event_id,
                outcome=str(outcome),
                amount=amount,
                source="web",
            )
        except DreamAgentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response(
                {"detail": str(exc) or "DreamLens could not place this trade."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {
                "trade": TradeSerializer(trade).data,
                "trade_id": trade.pk,
                "tx_hash": trade.transaction_hash,
                "via_smart_account": True,
            },
            status=status.HTTP_201_CREATED,
        )
