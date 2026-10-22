"""Optional OpenTelemetry / Application Insights configuration."""
from __future__ import annotations

import os


def configure() -> bool:
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not conn:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(connection_string=conn)
        return True
    except Exception:  # noqa: BLE001
        return False

