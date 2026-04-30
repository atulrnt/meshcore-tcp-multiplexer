import argparse
import asyncio
import logging

from mux import MeshCoreMux
from store import MessageStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MeshCore WiFi companion TCP multiplexer"
    )
    parser.add_argument("--companion-host", default="127.0.0.1", metavar="HOST")
    parser.add_argument("--companion-port", type=int, default=5000, metavar="PORT")
    parser.add_argument("--listen-host", default="0.0.0.0", metavar="HOST")
    parser.add_argument("--listen-port", type=int, default=5001, metavar="PORT")
    parser.add_argument(
        "--queue-depth",
        type=int,
        default=256,
        metavar="N",
        help="max queued frames before oldest is dropped (default: 256)",
    )
    parser.add_argument(
        "--store",
        metavar="FILE",
        help="enable store-and-forward using FILE as the SQLite database "
             "(e.g. --store messages.db). When set, private and channel "
             "messages received from the companion are persisted; clients "
             "that send SYNC_NEXT_MESSAGE receive any messages they missed "
             "since their last session before live forwarding resumes.",
    )
    parser.add_argument(
        "--debug", action="store_true", help="enable debug frame logging"
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
    )

    asyncio.run(mux.run())


if __name__ == "__main__":
    main()
