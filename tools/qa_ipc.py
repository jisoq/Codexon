"""Acknowledged commands over an already-connected QA local socket."""
import time


def command_reply(client, command, expected, timeout_ms=3000):
    deadline = time.monotonic() + timeout_ms / 1000
    def remaining():
        return max(0, int((deadline - time.monotonic()) * 1000))

    if client.write(command) != len(command):
        raise RuntimeError(f'GUI command write failed: {client.errorString()}')
    # A completed write can make waitForBytesWritten return false on Windows.
    # Wait only while data remains queued, then require the actual acknowledgement.
    while client.bytesToWrite():
        left = remaining()
        if not left or (not client.waitForBytesWritten(left) and client.bytesToWrite()):
            raise RuntimeError(f'GUI command delivery failed: {client.errorString()}')
    reply = b''
    while reply != expected:
        reply += bytes(client.readAll())
        if reply == expected:
            break
        if not expected.startswith(reply):
            raise RuntimeError(f'GUI command reply mismatch: {reply!r}')
        left = remaining()
        if not left or (not client.waitForReadyRead(left) and not client.bytesAvailable()):
            raise RuntimeError(f'GUI command reply incomplete: {reply!r}; {client.errorString()}')
    return reply
