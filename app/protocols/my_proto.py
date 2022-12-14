import asyncio
import logging
from http import HTTPStatus
from typing import Mapping, Iterable
from urllib.parse import unquote

import h11
from uvicorn.logging import TRACE_LOG_LEVEL
from uvicorn.protocols.utils import get_local_addr, get_remote_addr, is_ssl
from websockets.connection import OPEN
from websockets.extensions.permessage_deflate import ServerPerMessageDeflateFactory
from websockets.frames import OP_TEXT, OP_CLOSE
from websockets.server import ServerConnection, Request


class IudeenProto(asyncio.Protocol):
    def __init__(self, config, server_state, _loop=None):
        if not config.loaded:
            config.load()

        self.config = config
        self.app = config.loaded_app
        self.loop = _loop or asyncio.get_event_loop()
        self.logger = logging.getLogger("uvicorn.error")
        self.root_path = config.root_path

        # Shared server state
        self.connections = server_state.connections
        self.tasks = server_state.tasks

        # Connection state
        self.transport = None
        self.server = None
        self.client = None
        self.scheme = None

        # WebSocket state
        self.connect_event = None
        self.queue = asyncio.Queue()
        self.handshake_complete = False
        self.close_sent = False

        extensions = []
        if self.config.ws_per_message_deflate:
            extensions.append(ServerPerMessageDeflateFactory())

        self.conn = ServerConnection(
            max_size=self.config.ws_max_size,
            extensions=extensions
        )

        self.read_paused = False
        self.writable = asyncio.Event()
        self.writable.set()

        # Buffers
        self.bytes = b""
        self.text = ""

    # Protocol interface

    def connection_made(self, transport):
        self.connections.add(self)
        self.transport = transport
        self.server = get_local_addr(transport)
        self.client = get_remote_addr(transport)
        self.scheme = "wss" if is_ssl(transport) else "ws"

        if self.logger.level <= TRACE_LOG_LEVEL:
            prefix = "%s:%d - " % tuple(self.client) if self.client else ""
            self.logger.log(TRACE_LOG_LEVEL, "%sWebSocket connection made", prefix)

    def connection_lost(self, exc):
        if exc is not None:
            self.queue.put_nowait({"type": "websocket.disconnect"})
        self.connections.remove(self)

        if self.logger.level <= TRACE_LOG_LEVEL:
            prefix = "%s:%d - " % tuple(self.client) if self.client else ""
            self.logger.log(TRACE_LOG_LEVEL, "%sWebSocket connection lost", prefix)

        if exc is None:
            self.transport.close()

    def eof_received(self):
        pass

    def data_received(self, data):
        self.conn.receive_data(data)
        self.handle_events()

    async def async_data_received(self, data_to_send, events_to_process):
        if self.conn.state == OPEN and len(data_to_send) > 0:
            # receiving data can generate data to send (eg, pong for a ping)
            # send connection.data_to_send()
            await self.transport.write(data_to_send)
        if len(events_to_process) > 0:
            self.handle_events()

    def handle_events(self):
        events_to_process = self.conn.events_received()
        for event in events_to_process:
            if isinstance(event, Request):
                self.handle_connect(event)
            elif event.opcode == OP_TEXT:
                self.handle_text(event)
            elif event.opcode == OP_CLOSE:
                self.handle_close(event)

    def pause_writing(self):
        """
        Called by the transport when the write buffer exceeds the high water mark.
        """
        self.writable.clear()

    def resume_writing(self):
        """
        Called by the transport when the write buffer drops below the low water mark.
        """
        self.writable.set()

    def shutdown(self):
        self.queue.put_nowait({"type": "websocket.disconnect", "code": 1012})
        self.conn.send_close(code=1012)
        self.transport.write(self.conn.data_to_send())
        self.transport.close()

    def on_task_complete(self, task):
        self.tasks.discard(task)

    # Event handlers

    def handle_connect(self, event):
        self.connect_event = event
        headers = event.headers
        # headers += [(key.lower(), value) for key, value in event.headers.get("extra_headers", [])]
        headers = [(k.lower(), v) for k, v in headers.items()]
        raw_path, _, query_string = event.path.partition("?")

        subprotocols = []
        for header in event.headers.get_all("Sec-WebSocket-Protocol"):
            subprotocols.extend([token.strip() for token in header.split(",")])

        self.scope = {
            "type": "websocket",
            "asgi": {"version": self.config.asgi_version, "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": self.scheme,
            "server": self.server,
            "client": self.client,
            "root_path": self.root_path,
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
            "headers": headers,
            "subprotocols": subprotocols,
        }
        response = self.conn.accept(event)
        self.conn.send_response(response)
        self.queue.put_nowait({"type": "websocket.connect"})
        task = self.loop.create_task(self.run_asgi())
        task.add_done_callback(self.on_task_complete)
        self.tasks.add(task)

    def handle_no_connect(self, event):
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"connection", b"close"),
        ]
        msg = h11.Response(status_code=400, headers=headers, reason="Bad Request")
        output = self.conn.send(msg)
        msg = h11.Data(data=event.reason.encode("utf-8"))
        output += self.conn.send(msg)
        msg = h11.EndOfMessage()
        output += self.conn.send(msg)
        self.transport.write(output)
        self.transport.close()

    def handle_text(self, event):
        self.text = event.data
        self.queue.put_nowait({"type": "websocket.receive", "text": self.text})
        for data in self.conn.data_to_send():
            self.transport.write(data)

    def handle_bytes(self, event):
        self.bytes += event.data
        if event.message_finished:
            self.queue.put_nowait({"type": "websocket.receive", "bytes": self.bytes})
            self.bytes = b""
            if not self.read_paused:
                self.read_paused = True
                self.transport.pause_reading()

    def handle_close(self, event):
        if self.conn.state.CLOSING:
            for data in self.conn.data_to_send():
                self.transport.write(data)
        self.queue.put_nowait({"type": "websocket.disconnect", "code": 1005})
        self.transport.close()

    def handle_ping(self, event):
        self.transport.write(self.conn.send(event.response()))

    def send_500_response(self):
        print("500 Error")

    async def run_asgi(self):
        try:
            result = await self.app(self.scope, self.receive, self.send)
        except BaseException as exc:
            msg = "Exception in ASGI application\n"
            self.logger.error(msg, exc_info=exc)
            if not self.handshake_complete:
                self.send_500_response()
            self.transport.close()
        else:
            if not self.handshake_complete:
                msg = "ASGI callable returned without completing handshake."
                self.logger.error(msg)
                self.send_500_response()
                self.transport.close()
            elif result is not None:
                msg = "ASGI callable should return None, but returned '%s'."
                self.logger.error(msg, result)
                self.transport.close()

    async def send(self, message):
        await self.writable.wait()

        message_type = message["type"]

        if not self.handshake_complete:
            if message_type == "websocket.accept":
                self.logger.info(
                    '%s - "WebSocket %s" [accepted]',
                    self.scope["client"],
                    self.scope["path"],
                )
                self.handshake_complete = True
                for data in self.conn.data_to_send():
                    self.transport.write(data)

            elif message_type == "websocket.close":
                self.queue.put_nowait({"type": "websocket.disconnect", "code": None})
                self.logger.info(
                    '%s - "WebSocket %s" 403',
                    self.scope["client"],
                    self.scope["path"],
                )
                self.handshake_complete = True
                self.close_sent = True
                msg = self.conn.reject(status=HTTPStatus.FORBIDDEN, text="Reject")
                self.conn.send_response(msg)
                self.transport.write(self.conn.data_to_send())
                self.transport.close()

            else:
                msg = (
                    "Expected ASGI message 'websocket.accept' or 'websocket.close', "
                    "but got '%s'."
                )
                raise RuntimeError(msg % message_type)

        elif not self.close_sent:
            if message_type == "websocket.send":
                bytes_data = message.get("bytes")
                text_data = message.get("text")
                data = text_data if bytes_data is None else bytes_data

                if isinstance(data, str):
                    self.conn.send_text(data.encode("utf-8"))

                elif isinstance(data, (bytes, bytearray, memoryview)):
                    self.conn.send_binary(data)

                elif isinstance(message, Mapping):
                    # Catch a common mistake -- passing a dict to send().
                    raise TypeError("data is a dict-like object")

                elif isinstance(message, Iterable):
                    # Fragmented message -- regular iterator.
                    raise NotImplementedError(
                        "Fragmented websocket messages are not supported."
                    )
                else:
                    raise TypeError("Websocket data must be bytes, str.")

                if not self.transport.is_closing():
                    for data in self.conn.data_to_send():
                        self.transport.write(data)

            elif message_type == "websocket.close":
                self.close_sent = True
                code = message.get("code", 1000)
                reason = message.get("reason", "") or ""
                self.queue.put_nowait({"type": "websocket.disconnect", "code": code})
                self.conn.send_close(code=code, reason=reason)
                if not self.transport.is_closing():
                    self.transport.write(self.conn.data_to_send())
                    self.transport.close()

            else:
                msg = (
                    "Expected ASGI message 'websocket.send' or 'websocket.close',"
                    " but got '%s'."
                )
                raise RuntimeError(msg % message_type)

        else:
            msg = "Unexpected ASGI message '%s', after sending 'websocket.close'."
            raise RuntimeError(msg % message_type)

    async def receive(self):
        message = await self.queue.get()
        if self.read_paused and self.queue.empty():
            self.read_paused = False
            self.transport.resume_reading()
        return message
