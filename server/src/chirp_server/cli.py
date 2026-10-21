"""Local CLI. Pairing is the only command that deliberately prints a credential."""

import argparse
import logging
import os
import plistlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .config import Settings, load_token
from .retention import Retention

LABEL = "dev.chirp.server"


def onnx_thread_count(value):
    value = int(value)
    if not 0 <= value <= 2**31 - 1:
        raise argparse.ArgumentTypeError("must be an integer between 0 and 2147483647")
    return value


def service(action, arguments=()):
    destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    target = f"gui/{os.getuid()}"
    if action == "uninstall":
        subprocess.run(
            ["launchctl", "bootout", target + "/" + LABEL],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        destination.unlink(missing_ok=True)
        return
    # Explicit user command only; never called on server startup or by tests.
    executable = str(Path(sys.executable).absolute())
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as stream:
        plistlib.dump(
            {
                "Label": LABEL,
                "ProgramArguments": [executable, "-m", "chirp_server", *arguments],
                "RunAtLoad": True,
                "KeepAlive": True,
                "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": "/dev/null",
            },
            stream,
        )
    subprocess.run(["launchctl", "bootstrap", target, str(destination)], check=True)


def parser():
    result = argparse.ArgumentParser(description="Private local streaming dictation")
    result.add_argument("--config", type=Path)
    result.add_argument("--model-dir", type=Path)
    result.add_argument("--engine", choices=["mlx", "onnx"], default="mlx")
    for kind in ("intra", "inter"):
        result.add_argument(
            f"--onnx-{kind}-op-num-threads",
            type=onnx_thread_count,
            default=None,
            help=f"Parakeet ONNX {kind}-op threads: 0=automatic; omitted=use config/default",
        )
    result.add_argument(
        "--precision", choices=["float32", "float16", "bfloat16"], default="float32"
    )
    result.add_argument("--streaming", choices=["incremental", "rolling"], default="rolling")
    result.add_argument("--finalization", choices=["full", "segments"], default="full")
    result.add_argument("--debug-retain", action="store_true")
    result.add_argument("--port", type=int, default=7821)
    result.add_argument("--root-path", default="")
    commands = result.add_subparsers(dest="command")
    pair = commands.add_parser("pair")
    pair.add_argument("--url", default="http://127.0.0.1:7821")
    model = commands.add_parser("model")
    model.add_argument("action", choices=["fetch"])
    model.add_argument("--engine", choices=["mlx", "onnx"], default="mlx")
    model.add_argument("--cleanup", nargs="?", const="", default=None, metavar="ID")
    debug = commands.add_parser("debug")
    debug.add_argument("action", choices=["purge"])
    check = commands.add_parser("doctor")
    check.add_argument("--quick", action="store_true", help="skip the real model load and decode")
    launch = commands.add_parser("service")
    launch.add_argument("action", choices=["install", "uninstall"])
    setup = commands.add_parser("setup", help="local Mac setup with JSON progress")
    setup.add_argument(
        "action", choices=["status", "download", "start", "refresh", "add", "select"]
    )
    setup.add_argument("--model", choices=["speech", "cleanup"], default="speech")
    setup.add_argument("--choice", help="catalog id (including its pinned revision)")
    setup.add_argument("--repository", help="Hugging Face owner/repository for add")
    return result


def main():
    args = parser().parse_args()
    if args.command == "setup":
        from .setup import run

        raise SystemExit(
            run(args.action, args.model, args.config, args.port, args.choice, args.repository)
        )
    if args.command == "doctor":
        from .doctor import doctor

        # Read-only diagnostics run before config parsing so a broken config is reported,
        # not raised.
        raise SystemExit(doctor(args))
    settings = Settings.read(args.config)
    if args.command == "pair":
        import segno

        url = urlsplit(args.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise SystemExit("Pair URL must be an HTTP(S) base URL without credentials")
        uri = "chirp://pair?" + urlencode(
            {"url": args.url, "token": load_token(settings.config_dir)}
        )
        print(uri)
        segno.make(uri).terminal(compact=True)
    elif args.command == "model":
        if args.cleanup is not None:
            from .cleanup_models import fetch

            print(fetch(args.cleanup))
        else:
            from .engine.models import fetch

            print(fetch(args.engine))
    elif args.command == "debug":
        print(f"Purged {Retention(settings).purge(all_files=True)} debug files")
    elif args.command == "service":
        forwarded = ["--port", str(args.port)] if args.port != 7821 else []
        forwarded += ["--root-path", args.root_path] if args.root_path else []
        service(args.action, forwarded)
    else:
        import uvicorn

        from .app import accept_prefix, create_app

        if args.model_dir is not None:
            settings.model_dir = args.model_dir
        settings.engine = args.engine
        settings.precision = args.precision
        settings.streaming = args.streaming
        settings.finalization = args.finalization
        settings.onnx = replace(
            settings.onnx,
            **{
                f"{kind}_op_num_threads": value
                for kind in ("intra", "inter")
                if (value := getattr(args, f"onnx_{kind}_op_num_threads")) is not None
            },
        )
        settings.debug_retain |= args.debug_retain
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        # httpx logs can contain endpoint queries/credentials; library logs are not diagnostics.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            uvicorn.run(
                accept_prefix(create_app(settings), args.root_path),
                host="127.0.0.1",
                port=args.port,
                root_path=args.root_path,
                access_log=False,
                ws_max_size=999999,
                proxy_headers=False,
            )
        except Exception:
            # Model/config exceptions may contain paths or data; print only a safe diagnosis.
            raise SystemExit(
                "Server startup failed; check configuration and cached model"
            ) from None
