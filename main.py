import argparse
import asyncio
import logging

from mux import MeshCoreMux


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
        "--debug", action="store_true", help="enable debug frame logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    mux = MeshCoreMux(
        companion_host=args.companion_host,
        companion_port=args.companion_port,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        queue_depth=args.queue_depth,
    )

    asyncio.run(mux.run())


if __name__ == "__main__":
    main()
