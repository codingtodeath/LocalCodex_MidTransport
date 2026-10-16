"""Gateway replacement that does not recover another live process's requests."""
import argparse
import asyncio
from pathlib import Path
import uvicorn
from codex_relay.cli import _services
from codex_relay.config import load_config
from codex_relay.app import create_app
from .live_limits import apply_gateway_limits


class LiveAdmission:
    """Keep incoming requests connected during a bounded, idle Worker handoff."""
    def __init__(self, app, services, pause_file=None):
        self.app, self.services, self.pause_file = app, services, pause_file

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            if scope['method'] == 'POST' and scope['path'] == '/v1/responses':
                while self.pause_file is not None and self.pause_file.exists():
                    # Do not consume receive(): request input stays intact.
                    await asyncio.sleep(.05)
            apply_gateway_limits(self.services)
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--rolling-activation-file")
    parser.add_argument("--admission-pause-file")
    args = parser.parse_args()
    config = load_config(args.config)
    activation = Path(args.rolling_activation_file) if args.rolling_activation_file else None
    if activation and (activation.with_suffix('.finished').exists() or
            (activation.name == 'linux-gateway.activate' and activation.with_name("migration.finished").exists())):
        activation = None  # Ordinary restarts must perform crash recovery.
    services = _services(config, create=False)
    app = create_app(config, services=services, rolling_activation_file=activation)
    app.add_middleware(LiveAdmission, services=services,
        pause_file=Path(args.admission_pause_file) if args.admission_pause_file else None)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, workers=1,
        log_level="warning", access_log=False, proxy_headers=False, server_header=False,
        timeout_graceful_shutdown=1800)


if __name__ == "__main__":
    main()
