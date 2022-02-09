import enum
import json
import typing

from starlette.requests import HTTPConnection, empty_receive, empty_send
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send


class WebSocketState(enum.Enum):
    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocketDisconnect(Exception):
    def __init__(self, code: int = 1000) -> None:
        self.code = code


class WebSocket(HTTPConnection):
    def __init__(self, scope: Scope, receive: Receive = empty_receive, send: Send = empty_send) -> None:
        super().__init__(scope)
        assert scope["type"] == "websocket"
        self._receive = receive
        self._send = send
        self.client_state = WebSocketState.CONNECTING
        self.application_state = WebSocketState.CONNECTING

    async def receive(self) -> Message:
        """
        Receive ASGI websocket messages, ensuring valid state transitions.
        """
        if self.client_state == WebSocketState.CONNECTING:
            message = await self._receive()
            message_type = message["type"]
            assert message_type == "websocket.connect"
            self.client_state = WebSocketState.CONNECTED
            return message
        elif self.client_state == WebSocketState.CONNECTED:
            message = await self._receive()
            message_type = message["type"]
            assert message_type in {"websocket.receive", "websocket.disconnect"}
            if message_type == "websocket.disconnect":
                self.client_state = WebSocketState.DISCONNECTED
            return message
        else:
            raise RuntimeError(
                'Cannot call "receive" once a disconnect message has been received.'
            )

    async def send(self, message: Message) -> None:
        """
        Send ASGI websocket messages, ensuring valid state transitions.
        """
        if self.application_state == WebSocketState.CONNECTING:
            message_type = message["type"]
            assert message_type in {"websocket.accept", "websocket.close"}
            if message_type == "websocket.close":
                self.application_state = WebSocketState.DISCONNECTED
            else:
                self.application_state = WebSocketState.CONNECTED
            await self._send(message)
        elif self.application_state == WebSocketState.CONNECTED:
            message_type = message["type"]
            assert message_type in {"websocket.send", "websocket.close"}
            if message_type == "websocket.close":
                self.application_state = WebSocketState.DISCONNECTED
            await self._send(message)
        else:
            raise RuntimeError('Cannot call "send" once a close message has been sent.')

    async def accept(self, subprotocol: str = None) -> None:
        if self.client_state == WebSocketState.CONNECTING:
            # If we haven't yet seen the 'connect' message, then wait for it first.
            await self.receive()
        await self.send({"type": "websocket.accept", "subprotocol": subprotocol})

    def _raise_on_disconnect(self, message: Message) -> None:
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message["code"])

    async def receive_text(self) -> str:
        assert self.application_state == WebSocketState.CONNECTED
        message = await self.receive()
        self._raise_on_disconnect(message)
        return message["text"]

    async def receive_bytes(self) -> bytes:
        assert self.application_state == WebSocketState.CONNECTED
        message = await self.receive()
        self._raise_on_disconnect(message)
        return message["bytes"]

    async def receive_json(self, mode: str = "text") -> typing.Any:
        assert mode in ["text", "binary"]
        assert self.application_state == WebSocketState.CONNECTED
        message = await self.receive()
        self._raise_on_disconnect(message)

        if mode == "text":
            text = message["text"]
        else:
            text = message["bytes"].decode("utf-8")
        return json.loads(text)

    async def iter_text(self) -> typing.AsyncIterator[str]:
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            pass

    async def iter_bytes(self) -> typing.AsyncIterator[bytes]:
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            pass

    async def iter_json(self) -> typing.AsyncIterator[typing.Any]:
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            pass

    async def send_text(self, data: str) -> None:
        await self.send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        await self.send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: typing.Any, mode: str = "text") -> None:
        assert mode in ["text", "binary"]
        text = json.dumps(data)
        if mode == "text":
            await self.send({"type": "websocket.send", "text": text})
        else:
            await self.send({"type": "websocket.send", "bytes": text.encode("utf-8")})

    async def close(self, code: int = 1000) -> None:
        await self.send({"type": "websocket.close", "code": code})


class WebsocketDenialResponse:
    """
    Represents a failure to stabilish a websocket connection 

    If a Response is provided and the ASGI server supports the Websocket Denial Response extension,
    that is sent to the client. 
    Otherwise a standard 'close' event is sent, resulting in a generic HTTP 403 error
    """
    def __init__(self, response: Response = None) -> None:
        self.response = response

    message_type_map = {
        "http.response.start": "websocket.http.response.start",
        "http.response.body": "websocket.http.response.body",
        "websocket.disconnect": "http.disconnect",
    }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if Response is None or (scope["type"] == "websocket" and "websocket.http.response" not in scope.get("extensions", {})):
            # This is a websocket connection and Websocket Denial Response is not supported -- just close it
            await send({"type": "websocket.close"})

        else:
            async def xsend(msg: Message) -> None:
                new_type = self.message_type_map.get(msg["type"])
                if new_type is not None:
                    msg["type"] = new_type
                    await send(msg)

            async def xreceive() -> Message:
                while True:
                    msg = await receive()
                    new_type = self.message_type_map.get(msg["type"])
                    if new_type is not None:
                        msg["type"] = new_type
                        return msg

            await self.response(scope, xreceive, xsend)
