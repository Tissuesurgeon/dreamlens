from django.urls import path

from apps.core import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("explore/", views.explore, name="explore"),
    path("events/<int:pk>/", views.event_detail, name="event_detail"),
    path("following/", views.following, name="following"),
    path("following/activity/", views.copy_activity, name="copy_activity"),
    path("following/<int:pk>/settings/", views.copy_settings, name="copy_settings"),
    path("traders/<int:pk>/", views.trader_detail, name="trader_detail"),
    path("portfolio/", views.portfolio, name="portfolio"),
    path("agent/", views.dream_agent, name="dream_agent"),
    path("agent/activate/", views.dream_agent_activate, name="dream_agent_activate"),
    path("agent/skips/", views.dream_agent_skips, name="dream_agent_skips"),
]
