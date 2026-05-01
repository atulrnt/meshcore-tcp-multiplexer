import argparse
import asyncio
import logging
import os

from mux import MeshCoreMux
from store import MessageStore

_TRUTHY = {"1", "true", "yes"}


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").lower() in _TRUTHY


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MeshCore WiFi companion TCP multiplexer"
    )
    parser.add_argument(
        "--companion-host",
        default=os.environ.get("COMPANION_HOST", "127.0.0.1"),
        metavar="HOST",
    )
    parser.add_argument(
        "--companion-port",
        type=int,
        default=int(os.environ.get("COMPANION_PORT", 5000)),
        metavar="PORT",
    )
    parser.add_argument(
        "--listen-host",
        default=os.environ.get("LISTEN_HOST", "0.0.0.0"),
        metavar="HOST",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.environ.get("LISTEN_PORT", 5001)),
        metavar="PORT",
    )
    parser.add_argument(
        "--queue-depth",
        type=int,
        default=int(os.environ.get("QUEUE_DEPTH", 256)),
        metavar="N",
        help="max queued frames before oldest is dropped (default: 256)",
    )
    parser.add_argument(
        "--store",
        default=os.environ.get("STORE"),
        metavar="FILE",
        help="enable store-and-forward using FILE as the SQLite database "
             "(e.g. --store messages.db). When set, private and channel "
             "messages received from the companion are persisted; clients "
             "that send SYNC_NEXT_MESSAGE receive any messages they missed "
             "since their last session before live forwarding resumes.",
    )
    parser.add_argument(
        "--beacon",
        type=float,
        default=float(os.environ.get("BEACON", 0)) or None,
        metavar="SECONDS",
        help="send a channel message every SECONDS seconds (disabled if omitted)",
    )
    parser.add_argument(
        "--beacon-channel",
        type=int,
        default=int(os.environ.get("BEACON_CHANNEL", 0)),
        metavar="INDEX",
        help="channel slot (0-7) to beacon on (default: 0 = public)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=_env_bool("DEBUG"),
        help="enable debug frame logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    store = MessageStore(args.store) if args.store else None
    if store:
        logging.getLogger(__name__).info("store-and-forward enabled: %s", args.store)

    mux = MeshCoreMux(
        companion_host=args.companion_host,
        companion_port=args.companion_port,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        queue_depth=args.queue_depth,
        store=store,
        beacon=args.beacon,
        beacon_channel=args.beacon_channel,
    )

    asyncio.run(mux.run())


if __name__ == "__main__":
    main()
