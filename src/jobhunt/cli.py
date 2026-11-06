import argparse
import sys
from collections.abc import Callable

# Later tasks add a subcommand by writing one `register_x(sub)` function and appending it here.
Registrar = Callable[[argparse._SubParsersAction], None]
REGISTRARS: list[Registrar] = []

from . import importer  # noqa: E402

REGISTRARS.append(importer.register)

from . import daily  # noqa: E402

REGISTRARS.append(daily.register)


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
    p = sub.add_parser("serve", help="Run the local web UI on 127.0.0.1")
    def port_number(v: str) -> int:
        if not v.isdigit() or not 1 <= int(v) <= 65535:
            raise argparse.ArgumentTypeError(f"invalid port {v!r}: use a number from 1 to 65535")
        return int(v)

    p.add_argument("--port", type=port_number, default=8000)
    p.add_argument("--open", action="store_true", help="open the browser once the app starts")

    def handler(args) -> int:
        import errno
        import socket
        import threading
        import webbrowser

        import uvicorn

        from .config import load_settings
        from .web.app import create_app

        port = args.port

        def busy() -> int:
            print(f"Port {port} is busy — try `jobhunt serve --port {port + 2}`")
            return 1

        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return busy()
            raise
        finally:
            probe.close()
        app = create_app(load_settings())
        if args.open:
            threading.Timer(1.0, webbrowser.open, [f"http://127.0.0.1:{port}"]).start()
        try:
            uvicorn.run(app, host="127.0.0.1", port=port)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                return busy()
            raise
        return 0

    p.set_defaults(handler=handler)


REGISTRARS.append(_register_serve)

from . import pipeline  # noqa: E402

REGISTRARS.append(pipeline.register)

from .gmail import sync as gmail_sync  # noqa: E402

REGISTRARS.append(gmail_sync.register)

from . import tailor  # noqa: E402

REGISTRARS.append(tailor.register)


def _register_add(sub) -> None:
    p = sub.add_parser("add", help="Add a job from a link and tailor your resume to it")
    p.add_argument("url")
    p.add_argument("--text-file", help="posting text to use when the page can't be read")

    def handler(args) -> int:
        from pathlib import Path

        from . import db
        from .config import load_settings
        from .sources import by_link
        from .tailor import tailor_job

        settings = load_settings()
        text = None
        if args.text_file:
            try:
                text = Path(args.text_file).read_text()
            except (OSError, UnicodeDecodeError) as exc:
                print(f"Couldn't read {args.text_file}: {exc}")
                return 1
        conn = db.connect(settings.db_path)
        try:
            try:
                job_id, created = by_link.add_job(conn, args.url, settings, text=text)
            except by_link.LinkError as exc:
                print(exc)
                if exc.needs_text:
                    print("Save the posting text to a file and rerun with --text-file FILE.")
                return 1
            job = db.get_job(conn, job_id)
            label = "Added job" if created else "Already tracked: job"
            print(f"{label} {job_id}: {job['company']} — {job['title']}")
            result = tailor_job(conn, job_id, settings, progress=lambda msg: print(msg, flush=True))
        finally:
            conn.close()
        if result.error:
            print(f"Not tailored: {result.error}")
            return 1
        print(f"Saved: {result.path}")
        return 0

    p.set_defaults(handler=handler)


REGISTRARS.append(_register_add)

from . import migrate  # noqa: E402

REGISTRARS.append(migrate.register)
