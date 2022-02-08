import asyncio
import http
from typing import Callable, Any, Optional, Mapping, Dict, Type, Union, Awaitable


from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection, Request
from starlette.responses import PlainTextResponse, Response
from starlette.websockets import WebSocket
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = None) -> None:
        if detail is None:
            detail = http.HTTPStatus(status_code).phrase
        self.status_code = status_code
        self.detail = detail

    def __repr__(self) -> str:
        class_name = self.__class__.__name__
        return f"{class_name}(status_code={self.status_code!r}, detail={self.detail!r})"

ExceptionTypeOrStatusCode = Union[int, Type[Exception]]
ExceptionHandler = Callable[[HTTPConnection, Exception], Union[Response, Awaitable[Response]]]


class BaseExceptionMiddleware:
    def _is_scope_supported(self, scope: Scope) -> bool:
        if scope["type"] == "http":
            return True
        if scope["type"] == "websocket":
            return "websocket.http.response" in scope.get("extensions", {})
        return False

    def get_exception_handler(self, exc: Exception) -> Optional[ExceptionHandler]:
        raise NotImplementedError()

    def response_already_started(self, exc: Exception):
        # Response has already started, there isn't anything this middleware can do -- 
        # Just propagate the exception to the server / test client
        raise exc

    def propagate_exception(self, exc: Exception):
        # We always continue to raise the exception.
        # This allows servers to log the error, or allows test clients
        # to optionally raise the error within the test case.
        raise exc

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        print(f"ExceptionMiddleware.call. scope.type={scope['type']}, scope.extensions={scope['extensions']}")
        if not self._is_scope_supported(scope):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def sender(message: Message) -> None:
            nonlocal response_started

            if message["type"] in ["http.response.start", "websocket.accept", "websocket.close", "websocket.http.response.start"]:
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, sender)
        except Exception as exc:
            handler = self.get_exception_handler(exc)

            if handler is None:
                raise exc

            if response_started:
                self.response_already_started(exc)

            if scope["type"] == "http":
                http_connection = Request(scope, receive=receive) 
                sender = send
            else:
                http_connection = WebSocket(scope, receive=receive)
                async def sender(message: Message) -> None:
                    if message["type"] in ["http.response.start", "http.response.body"]:
                        message["type"] = "websocket." + message["type"]
                    await send(message)

            if asyncio.iscoroutinefunction(handler):
                response = await handler(http_connection, exc)
            else:
                response = await run_in_threadpool(handler, http_connection, exc)
            await response(scope, receive, sender)

            self.propagate_exception(exc)       


class ExceptionMiddleware(BaseExceptionMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        handlers: Mapping[ExceptionTypeOrStatusCode, ExceptionHandler] = None,
        debug: bool = False,
    ) -> None:
        self.app = app
        self.debug = debug  # TODO: We ought to handle 404 cases if debug is set.
        self._status_handlers: Dict[int, ExceptionHandler] = {}
        self._exception_handlers: Dict[Type[Exception], ExceptionHandler] = {HTTPException: self.http_exception}
        if handlers is not None:
            for key, value in handlers.items():
                self.add_exception_handler(key, value)

    def add_exception_handler(
        self,
        exc_class_or_status_code: ExceptionTypeOrStatusCode,
        handler: ExceptionHandler,
    ) -> None:
        if isinstance(exc_class_or_status_code, int):
            self._status_handlers[exc_class_or_status_code] = handler
        else:
            assert issubclass(exc_class_or_status_code, Exception)
            self._exception_handlers[exc_class_or_status_code] = handler

    def get_exception_handler(self, exc: Exception) -> Optional[ExceptionHandler]:
        if isinstance(exc, HTTPException):
            handler = self._status_handlers.get(exc.status_code)
            if handler:
                return handler
        for cls in type(exc).__mro__:
            if cls in self._exception_handlers:
                return self._exception_handlers[cls]
        return None

    def response_already_started(self, exc: Exception):
        # Just note that this exception would have been handled if the response hadn't started yet
        msg = "Caught handled exception, but response already started."
        raise RuntimeError(msg) from exc

    def propagate_exception(self, exc: Exception):
        # ExceptionMiddleware does not propagate exceptions to the server or test clients
        pass

    def http_exception(self, http_connection: HTTPConnection, exc: HTTPException) -> Response:
        if exc.status_code in {204, 304}:
            return Response(b"", status_code=exc.status_code)
        return PlainTextResponse(exc.detail, status_code=exc.status_code)
