"""Fixed ports of the SREGym track (#667 S-15)."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Ports:
    pg: int = 29510
    web: int = 29580
    prometheus: int = 29590
    jaeger: int = 29586
    #: The Loki port-forward's local end; only the read-only gateway's upstream.
    loki: int = 29531
    loki_gateway: int = 29532
    #: The Kafka port-forward's local end. Must equal the broker's advertised
    #: port: after the first metadata the client reconnects to ``kafka:9092``
    #: (C3, #567), which the host file maps to 127.0.0.1.
    kafka: int = 9092
    #: Where nothing listens: a missing observability service points here (H-11).
    unreachable: int = 29599
    conductor: int = 28000
    mcp: int = 29954
    sregym_proxy: int = 26443
    opspilot_proxy: int = 26444

    def all(self) -> dict[str, int]:
        return asdict(self)

    def per_trial(self) -> dict[str, int]:
        """Ports that must be free before a trial starts. The round's Loki
        gateway (started once per round) and the unreachable port are not
        in it."""
        # mcp: upstream main.py leaves its MCP port-forward running and
        # reclaims it on the next run (_kill_stale_port_forward); not ours.
        return {
            k: v
            for k, v in self.all().items()
            if k not in ("unreachable", "loki_gateway", "mcp")
        }
