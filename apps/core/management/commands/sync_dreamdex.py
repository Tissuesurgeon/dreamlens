"""Sync live DreamDEX Event Contracts into the local index."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from services.consensus_service import compute_consensus_for_all_active
from services.event_service import sync_events
from services.radar_service import generate_radar_signals
from services.trader_service import sync_traders_from_fills


class Command(BaseCommand):
    help = (
        "Pull live Event Contracts from DreamDEX (indexer + on-chain metadata), "
        "then refresh trader fills, radar, and consensus. No mock/seed data."
    )

    def handle(self, *args, **options):
        self.stdout.write("Syncing events from DreamDEX...")
        event_stats = sync_events()
        self.stdout.write(self.style.SUCCESS(f"  Events: {event_stats}"))

        self.stdout.write("Indexing trader fills from DreamDEX...")
        fill_stats = sync_traders_from_fills()
        self.stdout.write(self.style.SUCCESS(f"  Traders/fills: {fill_stats}"))

        self.stdout.write("Generating radar signals...")
        radar_stats = generate_radar_signals()
        self.stdout.write(self.style.SUCCESS(f"  Radar: {radar_stats}"))

        self.stdout.write("Computing consensus snapshots...")
        consensus_stats = compute_consensus_for_all_active()
        self.stdout.write(self.style.SUCCESS(f"  Consensus: {consensus_stats}"))

        self.stdout.write(self.style.SUCCESS("DreamDEX sync complete."))
