"""Own admission separately from accepted relays, including aborted Windows accepts."""
import asyncio
import errno
import socket


def peer_disconnected(error):
    # AcceptEx can complete after the connecting peer has already disappeared.
    return (getattr(error, 'winerror', None) in (64, 1236, 10053, 10054, 10057)
            or error.errno in (errno.ECONNABORTED, errno.ECONNRESET))


class RelayListener:
    """Public asyncio socket APIs keep a peer reset from closing the listening socket.

    Python 3.12's Proactor create_server callback closes its listener on any
    accept OSError. A living worker then cannot receive health or model calls.
    There is one accept owner, no idle polling, and at most 100 retries per second.
    The aiohttp runner continues to own and drain accepted connections.
    """
    def __init__(self, runner, port, diagnostics=None):
        self.runner = runner
        self.diagnostics = diagnostics
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.task = None
        try:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self.socket.setblocking(False)
            self.socket.bind(('127.0.0.1', port))
            self.socket.listen(128)
            self.port = self.socket.getsockname()[1]
            self.task = asyncio.create_task(self.accept())
        except BaseException:
            self.socket.close()
            raise

    async def accept(self):
        loop = asyncio.get_running_loop()
        while True:
            connection = None
            try:
                connection, _ = await loop.sock_accept(self.socket)
                connection.setblocking(False)
                await loop.connect_accepted_socket(self.runner.server, connection)
                connection = None  # Ownership moved to the aiohttp transport.
            except OSError as error:
                if not peer_disconnected(error):
                    if self.diagnostics:
                        self.diagnostics.fail(error, 'listener')
                    raise
                if self.diagnostics:
                    self.diagnostics.listener_retry(error)
                await asyncio.sleep(.01)
            finally:
                if connection is not None:
                    connection.close()

    async def wait(self, stop):
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((self.task, stopping), return_when=asyncio.FIRST_COMPLETED)
            if self.task.done():
                await self.task  # Fatal admission errors must reach managed recovery.
        finally:
            stopping.cancel()
            self.task.cancel()
            await asyncio.gather(stopping, self.task, return_exceptions=True)
            self.socket.close()
