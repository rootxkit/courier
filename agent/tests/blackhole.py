"""A local TCP proxy that can stall a connection without closing it.

Procedure B stops the receiver, which produces an immediate connection refusal.
A pulled cable produces silence: no RST, no FIN, a half-open connection that
looks alive to both ends. The relay must notice from missing pongs, and that is
a different code path which nothing else in the suite exercises.

This proxy sits between the relay and the sink and can be told to stop passing
bytes while holding both sockets open. Bytes are held rather than discarded, so
clearing the blackhole resumes the stream intact — which is what a brief
network stall actually does, since TCP retransmits rather than losing data.

It is a pessimistic model, not an identical one. A real cable pull takes the
interface down, and the OS may then fail pending sends outright, which is
easier to detect than silence. Passing here does not replace the hardware test.

    async with BlackholeProxy(sink_host, sink_port) as proxy:
        ...  # relay connects to proxy.port
        proxy.engage()   # bytes stop moving, sockets stay open
        proxy.clear()    # bytes flow again
"""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType

__all__ = ["BlackholeProxy"]

_CHUNK = 65536


class BlackholeProxy:
    """Forwards TCP to a target, with a switch that stalls the traffic."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target_host = target_host
        self._target_port = target_port
        self._server: asyncio.Server | None = None
        # Set means "bytes may flow". Cleared means blackholed: each pump
        # blocks before writing, which holds the data and applies backpressure
        # exactly as a stalled link would.
        self._open = asyncio.Event()
        self._open.set()
        self._tasks: set[asyncio.Task[None]] = set()
        self.connections = 0

    @property
    def port(self) -> int:
        assert self._server is not None, "proxy is not running"
        return int(self._server.sockets[0].getsockname()[1])

    def engage(self) -> None:
        """Stop passing bytes. Both sockets stay open."""
        self._open.clear()

    def clear(self) -> None:
        """Resume. Held bytes are delivered in order."""
        self._open.set()

    @property
    def blackholed(self) -> bool:
        return not self._open.is_set()

    async def __aenter__(self) -> BlackholeProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Let any blocked pump run to completion rather than hang on close.
        self._open.set()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            target_reader, target_writer = await asyncio.open_connection(
                self._target_host, self._target_port
            )
        except OSError:
            writer.close()
            return

        upstream = asyncio.create_task(self._pump(reader, target_writer))
        downstream = asyncio.create_task(self._pump(target_reader, writer))
        for task in (upstream, downstream):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        try:
            await asyncio.wait(
                {upstream, downstream}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (upstream, downstream):
                task.cancel()
            for closing in (writer, target_writer):
                closing.close()
                with contextlib.suppress(Exception):
                    await closing.wait_closed()

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                # A close must not cross the blackhole either. A pulled cable
                # does not deliver a FIN, and if the far end gives up first,
                # letting its disconnect through would hand the near end an
                # immediate close - which is Procedure B's failure mode, not
                # this one. Measuring that instead would be measuring the
                # wrong side's timeout.
                await self._open.wait()
                return
            # Blocks here while blackholed, holding the bytes rather than
            # dropping them, so the stream survives a stall that ends before
            # either side gives up on it.
            await self._open.wait()
            writer.write(data)
            await writer.drain()
