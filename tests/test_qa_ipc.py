"""Real local-socket acknowledgement races in packaged verification tools."""
import subprocess
import sys
import uuid

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket

from tools.qa_ipc import command_reply


SERVER = r'''
import sys
from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtNetwork import QLocalServer
app = QCoreApplication([])
server = QLocalServer()
assert server.listen(sys.argv[1]), server.errorString()
clients = []
def accept():
    client = server.nextPendingConnection()
    clients.append(client)
    def answer():
        assert bytes(client.readAll()) == b'verify-quit'
        mode = sys.argv[2]
        if mode == 'silent':
            return
        if mode == 'disconnect':
            client.disconnectFromServer()
            return
        if mode == 'fragmented':
            client.write(b'quit'); client.flush()
            QTimer.singleShot(50, lambda: (client.write(b'ting'), client.flush()))
        else:
            client.write(b'wrong' if mode == 'wrong' else b'quitting')
            client.flush()
    client.readyRead.connect(answer)
server.newConnection.connect(accept)
print('ready', flush=True)
QTimer.singleShot(5000, app.quit)
app.exec()
'''


@pytest.mark.parametrize('mode', ['normal', 'precompleted', 'fragmented', 'wrong', 'disconnect', 'silent'])
def test_command_requires_reply_even_when_write_already_completed(mode):
    app = QCoreApplication.instance() or QCoreApplication([])
    name = 'Codexon-QA-test-' + uuid.uuid4().hex
    process = subprocess.Popen([sys.executable, '-c', SERVER, name, mode],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    class CompletedSocket(QLocalSocket):
        def write(self, data):
            count = super().write(data)
            assert self.waitForReadyRead(2000)
            assert self.bytesToWrite() == 0
            assert not self.waitForBytesWritten(1)
            return count
    client = CompletedSocket() if mode == 'precompleted' else QLocalSocket()
    try:
        assert process.stdout.readline().strip() == 'ready'
        client.connectToServer(name)
        assert client.waitForConnected(2000), client.errorString()
        if mode in ('wrong', 'disconnect', 'silent'):
            with pytest.raises(RuntimeError, match='reply'):
                command_reply(client, b'verify-quit', b'quitting', timeout_ms=300)
        else:
            assert command_reply(client, b'verify-quit', b'quitting') == b'quitting'
    finally:
        client.abort()
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
