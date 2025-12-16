"""HTTP/JSON-RPC server for receiving notifications from remote systems."""

import asyncio
import logging
from aiohttp import web
from typing import Callable, Optional, Dict, Any

logger = logging.getLogger(__name__)


class NotificationServer:
    """Server for receiving notalone notification callbacks."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 0,  # 0 = auto-assign
        auth_token: Optional[str] = None
    ):
        """
        Initialize the notification server.

        Args:
            host: Host to bind to
            port: Port to bind to (0 for auto-assign)
            auth_token: Optional token for authentication
        """
        self.host = host
        self.port = port
        self.auth_token = auth_token
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # Handlers for different RPC methods
        self._handlers: Dict[str, Callable] = {}

    def register_handler(self, method: str, handler: Callable):
        """
        Register a handler for an RPC method.

        Args:
            method: RPC method name (e.g., "notalone.notify")
            handler: Async function to handle the call
        """
        self._handlers[method] = handler
        logger.debug(f"Registered handler for {method}")

    async def start(self):
        """Start the HTTP server."""
        self._app = web.Application()
        self._app.router.add_post("/notalone/notify", self._handle_notify)
        self._app.router.add_post("/notalone/register", self._handle_register)
        self._app.router.add_post("/notalone/unregister", self._handle_unregister)
        self._app.router.add_post("/notalone/list", self._handle_list)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        # Get actual port if auto-assigned
        if self.port == 0:
            # Access the actual server socket to get the assigned port
            if self._site and self._site._server:
                sock = self._site._server.sockets[0]
                self.port = sock.getsockname()[1]

        logger.info(f"NotificationServer listening on {self.host}:{self.port}")

    async def stop(self):
        """Stop the HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
        logger.info("NotificationServer stopped")

    def get_callback_url(self) -> str:
        """Get the callback URL for this server."""
        return f"http://{self.host}:{self.port}/notalone/notify"

    def _check_auth(self, params: Dict[str, Any]) -> bool:
        """Check if request is authenticated."""
        if not self.auth_token:
            return True  # No auth required

        provided_token = params.get("auth_token")
        return provided_token == self.auth_token

    async def _handle_notify(self, request: web.Request) -> web.Response:
        """Handle notalone.notify RPC call."""
        try:
            data = await request.json()
            params = data.get("params", {})

            if not self._check_auth(params):
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Authentication failed"
                    },
                    "id": data.get("id")
                }, status=403)

            # Call registered handler
            handler = self._handlers.get("notalone.notify")
            if not handler:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": "Method not found"
                    },
                    "id": data.get("id")
                }, status=404)

            result = await handler(params)

            return web.json_response({
                "jsonrpc": "2.0",
                "result": result,
                "id": data.get("id")
            })

        except Exception as e:
            logger.error(f"Error handling notify: {e}")
            return web.json_response({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": str(e)
                },
                "id": data.get("id") if "data" in locals() else None
            }, status=500)

    async def _handle_register(self, request: web.Request) -> web.Response:
        """Handle notalone.register RPC call."""
        try:
            data = await request.json()
            params = data.get("params", {})

            if not self._check_auth(params):
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Authentication failed"
                    },
                    "id": data.get("id")
                }, status=403)

            handler = self._handlers.get("notalone.register")
            if not handler:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": "Method not found"
                    },
                    "id": data.get("id")
                }, status=404)

            result = await handler(params)

            return web.json_response({
                "jsonrpc": "2.0",
                "result": result,
                "id": data.get("id")
            })

        except Exception as e:
            logger.error(f"Error handling register: {e}")
            return web.json_response({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": str(e)
                },
                "id": data.get("id") if "data" in locals() else None
            }, status=500)

    async def _handle_unregister(self, request: web.Request) -> web.Response:
        """Handle notalone.unregister RPC call."""
        try:
            data = await request.json()
            params = data.get("params", {})

            if not self._check_auth(params):
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Authentication failed"
                    },
                    "id": data.get("id")
                }, status=403)

            handler = self._handlers.get("notalone.unregister")
            if not handler:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": "Method not found"
                    },
                    "id": data.get("id")
                }, status=404)

            result = await handler(params)

            return web.json_response({
                "jsonrpc": "2.0",
                "result": result,
                "id": data.get("id")
            })

        except Exception as e:
            logger.error(f"Error handling unregister: {e}")
            return web.json_response({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": str(e)
                },
                "id": data.get("id") if "data" in locals() else None
            }, status=500)

    async def _handle_list(self, request: web.Request) -> web.Response:
        """Handle notalone.list RPC call."""
        try:
            data = await request.json()
            params = data.get("params", {})

            if not self._check_auth(params):
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32005,
                        "message": "Authentication failed"
                    },
                    "id": data.get("id")
                }, status=403)

            handler = self._handlers.get("notalone.list")
            if not handler:
                return web.json_response({
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32601,
                        "message": "Method not found"
                    },
                    "id": data.get("id")
                }, status=404)

            result = await handler(params)

            return web.json_response({
                "jsonrpc": "2.0",
                "result": result,
                "id": data.get("id")
            })

        except Exception as e:
            logger.error(f"Error handling list: {e}")
            return web.json_response({
                "jsonrpc": "2.0",
                "error": {
                    "code": -32000,
                    "message": str(e)
                },
                "id": data.get("id") if "data" in locals() else None
            }, status=500)
