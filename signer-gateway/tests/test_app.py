import asyncio
import json

from signer_gateway.app import clef_rpc


def test_clef_rpc_uses_newline_delimited_unix_json_rpc(monkeypatch):
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"jsonrpc":"2.0","id":1,"result":"6.1.0"}\n')
        reader.feed_eof()

        class Writer:
            def __init__(self):
                self.wire_request = b""

            def write(self, data):
                self.wire_request += data

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        writer = Writer()

        async def open_unix_connection(path, *, limit):
            assert path == "/run/clef.ipc"
            assert limit == 1_048_577
            return reader, writer

        monkeypatch.setattr(asyncio, "open_unix_connection", open_unix_connection)
        response = await clef_rpc(
            "/run/clef.ipc",
            {"jsonrpc": "2.0", "id": 1, "method": "account_version", "params": []},
            1,
        )
        assert response["result"] == "6.1.0"
        request = json.loads(writer.wire_request)
        assert request["method"] == "account_version"
        assert writer.wire_request.endswith(b"\n")

    asyncio.run(scenario())
