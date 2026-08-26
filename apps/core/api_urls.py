from django.urls import path

from apps.core.api.ai import AnalyzeEventView, ChatView, SearchView
from apps.core.api.wallet_auth import WalletLoginView, WalletLogoutView
from apps.core.api.agents import (
    DreamAgentEvaluationsView,
    DreamAgentGrantView,
    DreamAgentRevokeView,
    DreamAgentStatusView,
    SmartAccountDepositView,
    SmartAccountView,
)
from apps.core.api.copy import (
    CopyActivityView,
    CopyExecutionPrepareView,
    CopyExecutionSkipView,
    CopyPendingView,
    CopyRelationshipDetailView,
    CopyRelationshipListCreateView,
)
from apps.core.api.events import (
    EventConsensusView,
    EventDetailView,
    EventHistoryView,
    EventListView,
    EventRadarView,
)
from apps.core.api.portfolio import (
    PortfolioBalancesView,
    PortfolioPositionsView,
    PortfolioSummaryView,
    PositionRedeemConfirmView,
    PositionRedeemView,
)
from apps.core.api.traders import TraderDetailView, TraderHistoryView, TraderListView
from apps.core.api.telegram import TelegramLinkView, TelegramWebhookView
from apps.core.api.trading import ConfirmTradeView, PrepareTradeView, TradeDetailView, TradeListView

urlpatterns = [
    path("auth/wallet/", WalletLoginView.as_view(), name="api-auth-wallet"),
    path("auth/logout/", WalletLogoutView.as_view(), name="api-auth-logout"),
    # Events
    path("events/", EventListView.as_view(), name="api-events-list"),
    path("events/radar/", EventRadarView.as_view(), name="api-events-radar"),
    path("events/<int:pk>/", EventDetailView.as_view(), name="api-events-detail"),
    path("events/<int:pk>/history/", EventHistoryView.as_view(), name="api-events-history"),
    path("events/<int:pk>/consensus/", EventConsensusView.as_view(), name="api-events-consensus"),
    # Traders
    path("traders/", TraderListView.as_view(), name="api-traders-list"),
    path("traders/<int:pk>/", TraderDetailView.as_view(), name="api-traders-detail"),
    path("traders/<int:pk>/history/", TraderHistoryView.as_view(), name="api-traders-history"),
    # AI
    path("ai/chat/", ChatView.as_view(), name="api-ai-chat"),
    path("ai/analyze-event/", AnalyzeEventView.as_view(), name="api-ai-analyze"),
    path("ai/search/", SearchView.as_view(), name="api-ai-search"),
    # Trading
    path("trades/prepare/", PrepareTradeView.as_view(), name="api-trades-prepare"),
    path("trades/confirm/", ConfirmTradeView.as_view(), name="api-trades-confirm"),
    path("trades/", TradeListView.as_view(), name="api-trades-list"),
    path("trades/<int:pk>/", TradeDetailView.as_view(), name="api-trades-detail"),
    # Portfolio
    path("portfolio/", PortfolioSummaryView.as_view(), name="api-portfolio-summary"),
    path("portfolio/balances/", PortfolioBalancesView.as_view(), name="api-portfolio-balances"),
    path("portfolio/positions/", PortfolioPositionsView.as_view(), name="api-portfolio-positions"),
    path(
        "portfolio/positions/<int:pk>/redeem/",
        PositionRedeemView.as_view(),
        name="api-portfolio-redeem",
    ),
    path(
        "portfolio/positions/<int:pk>/redeem/confirm/",
        PositionRedeemConfirmView.as_view(),
        name="api-portfolio-redeem-confirm",
    ),
    # Copy / Smart Copy
    path("copy/", CopyRelationshipListCreateView.as_view(), name="api-copy-list"),
    path("copy/pending/", CopyPendingView.as_view(), name="api-copy-pending"),
    path("copy/activity/", CopyActivityView.as_view(), name="api-copy-activity"),
    path("copy/<int:pk>/", CopyRelationshipDetailView.as_view(), name="api-copy-detail"),
    path(
        "copy/executions/<int:pk>/skip/",
        CopyExecutionSkipView.as_view(),
        name="api-copy-exec-skip",
    ),
    path(
        "copy/executions/<int:pk>/prepare/",
        CopyExecutionPrepareView.as_view(),
        name="api-copy-exec-prepare",
    ),
    # DreamLens Smart Account + DreamAgent
    path("smart-account/", SmartAccountView.as_view(), name="api-smart-account"),
    path(
        "smart-account/deposit/",
        SmartAccountDepositView.as_view(),
        name="api-smart-account-deposit",
    ),
    path("agent/", DreamAgentStatusView.as_view(), name="api-agent-status"),
    path("agent/grant/", DreamAgentGrantView.as_view(), name="api-agent-grant"),
    path("agent/revoke/", DreamAgentRevokeView.as_view(), name="api-agent-revoke"),
    path(
        "agent/evaluations/",
        DreamAgentEvaluationsView.as_view(),
        name="api-agent-evaluations",
    ),
    path("telegram/link/", TelegramLinkView.as_view(), name="api-telegram-link"),
    path("telegram/webhook/", TelegramWebhookView.as_view(), name="api-telegram-webhook"),
]
