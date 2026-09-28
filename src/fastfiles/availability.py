"""Check for FastFiles itself, without sending saved pairing codes."""

from __future__ import annotations

import http.client
import threading
from concurrent.futures import ThreadPoolExecutor

from .locker import PROTOCOL_VERSION, Peer, PeerClient


def probe_peer(peer: Peer, timeout: float = 2) -> str:
    try:
        info = PeerClient(peer, "", timeout=timeout).info()
        if not isinstance(info.get("device_id"), str) or not isinstance(info.get("device_name"), str):
            return "Not FastFiles"
        return "Online" if info.get("protocol") == PROTOCOL_VERSION else "Update needed"
    except (OSError, ValueError, http.client.HTTPException):
        return "Offline"


def check_peers(peers: list[Peer], cancel: threading.Event | None = None) -> dict[tuple[str, int], str]:
    unique = {(peer.address, peer.port): peer for peer in peers}

    def probe(peer: Peer) -> str:
        return "Unchecked" if cancel is not None and cancel.is_set() else probe_peer(peer)

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="fastfiles-probe") as pool:
        return dict(zip(unique, pool.map(probe, unique.values())))
