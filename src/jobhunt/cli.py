import argparse
import sys
from collections.abc import Callable

# Later tasks add a subcommand by writing one `register_x(sub)` function and appending it here.
Registrar = Callable[[argparse._SubParsersAction], None]
REGISTRARS: list[Registrar] = []

from . import importer  # noqa: E402

REGISTRARS.append(importer.register)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunt", description="Local job-search tool")
    sub = parser.add_subparsers(dest="command")
    for register in REGISTRARS:
        register(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args) or 0


def main_entry() -> None:
    sys.exit(main())

from . import scan  # noqa: E402

REGISTRARS.append(scan.register)

from . import fit  # noqa: E402

REGISTRARS.append(fit.register)

from .people import finder  # noqa: E402

REGISTRARS.append(finder.register)


def _register_serve(sub) -> None:
    p = sub.add_parser("serve", help="Run the local web UI on 127.0.0.1:8000")

    def handler(args) -> int:
        import uvicorn

        from .config import load_settings
        from .web.app import create_app

        uvicorn.run(create_app(load_settings()), host="127.0.0.1", port=8000)
        return 0

    p.set_defaults(handler=handler)


REGISTRARS.append(_register_serve)

from . import pipeline  # noqa: E402

REGISTRARS.append(pipeline.register)

from .gmail import sync as gmail_sync  # noqa: E402

REGISTRARS.append(gmail_sync.register)
