import enum
import asyncio
import copy
import typing
import logging
from io import BytesIO
from dataclasses import dataclass

from ..backends import WebSocket
from ..exceptions import UnexpectedMessage, WebSocketClosed, WebSocketError
from ..types import ASGIApp, Message, WsRequest, Response


class WebSocketCycleState(enum.Enum):
    """
    The state of the ASGI WebSocket connection.

    * **CONNECTING** - Initial state. The ASGI application instance will be run with the
    connection scope containing the `websocket` type.
    * **HANDSHAKE** - The ASGI `websocket` connection with the application has been
    established, and a `websocket.connect` event has been pushed to the application
    queue. The application will respond by accepting or rejecting the connection.
    If rejected, a 403 response will be returned to the client, and it will be removed
    from API Gateway.
    * **RESPONSE** - Handshake accepted by the application. Data received in the API
    Gateway message event will be sent to the application. A `websocket.receive` event
    will be pushed to the application queue.
    * **DISCONNECTING** - The ASGI connection cycle is complete and should be
    disconnected from the application. A `websocket.disconnect` event will be pushed to
    the queue, and a response will be returned to the client connection.
    * **CLOSED** - The application has sent a `websocket.close` message. This will
    either be in response to a `websocket.disconnect` event or occurs when a connection
    is rejected in response to a `websocket.connect` event.
    """

    CONNECTING = enum.auto()
    HANDSHAKE = enum.auto()
    RESPONSE = enum.auto()
    DISCONNECTING = enum.auto()
    CLOSED = enum.auto()


@dataclass
class WebSocketCycle:
    """
    Manages the application cycle for an ASGI `websocket` connection.

    * **websocket** - A `WebSocket` connection handler interface for the selected
    `WebSocketBackend` subclass. Contains the ASGI connection `scope` and client
    connection identifier.
    * **state** - An enumerated `WebSocketCycleState` type that indicates the state of
    the ASGI connection.
    * **app_queue** - An asyncio queue (FIFO) containing messages to be received by the
    application.
    """

    request: WsRequest
    message_type: str
    connection_id: str
    state: WebSocketCycleState = WebSocketCycleState.CONNECTING

    def __post_init__(self) -> None:
        self.logger: logging.Logger = logging.getLogger("mangum.websocket")
        self.loop = asyncio.get_event_loop()
        self.app_queue: asyncio.Queue[typing.Dict[str, typing.Any]] = asyncio.Queue()
        self.body: BytesIO = BytesIO()
        self.response: Response = Response(200, [], b"")

    def __call__(self, app: ASGIApp, initial_body: bytes) -> Response:
        self.logger.debug("WebSocket cycle starting.")
        self.initial_body = initial_body
        # self.app_queue.put_nowait({"type": "websocket.connect"})
        asgi_instance = self.run(app)
        asgi_task = self.loop.create_task(asgi_instance)
        self.loop.run_until_complete(asgi_task)

        return self.response

    async def run(self, app: ASGIApp) -> None:
        """
        Calls the application with the `websocket` connection scope.
        """
        try:
            await app(self.request.scope, self.receive, self.send)
        except WebSocketClosed:
            self.response.status = 403
        except UnexpectedMessage:
            self.response.status = 500
        except BaseException as exc:
            self.logger.error("Exception in ASGI application", exc_info=exc)
            self.response.status = 500

    async def receive(self) -> Message:
        """
        Awaited by the application to receive ASGI `websocket` events.
        """
        return await self.app_queue.get()

    async def send(self, message: Message) -> None:
        """
        Awaited by the application to send ASGI `websocket` events.
        """
        return await self.app_queue.put(message)
