import os
from unittest.mock import patch

import grpc

from chalkcompute._volume import Volume, VolumeClient


class MockConnect:
    def __init__(self, api_server: str) -> None:
        self._api_server = api_server
        self._access_token = "token-123"
        self._env_id = "env-456"

    def _ensure_auth(self) -> None:
        pass


class TestVolumeConstructor:
    def test_accepts_explicit_client_and_name(self):
        client = object.__new__(VolumeClient)

        vol = Volume("my-data", client=client)

        assert vol.name == "my-data"
        assert vol._client is client

    def test_accepts_name_only_and_uses_from_env(self):
        client = object.__new__(VolumeClient)

        with patch.object(VolumeClient, "from_env", return_value=client) as from_env:
            vol = Volume("my-data")

        assert vol.name == "my-data"
        assert vol._client is client
        from_env.assert_called_once_with()

class TestVolumeClientFromEnv:
    def test_builds_tls_client_from_connect_auth(self):
        connect = MockConnect("https://api.chalk.ai")

        with patch.dict(os.environ, {"CHALK_API_SERVER": "https://api.chalk.ai"}):
            with patch(
                "chalkcompute._volume.ConnectClient", return_value=connect
            ) as connect_cls:
                with patch.object(connect, "_ensure_auth") as ensure_auth:
                    client = VolumeClient.from_env()

        assert client._target == "api.chalk.ai:443"
        assert client._metadata == [
            ("authorization", "Bearer token-123"),
            ("x-chalk-env-id", "env-456"),
            ("x-chalk-server", "go-api"),
        ]
        connect_cls.assert_called_once_with()
        ensure_auth.assert_called_once_with()
        client.close()

    def test_preserves_explicit_port(self):
        connect = MockConnect("https://api.chalk.ai:7443")

        with patch.dict(os.environ, {"CHALK_API_SERVER": "https://api.chalk.ai:7443"}):
            with patch("chalkcompute._volume.ConnectClient", return_value=connect):
                with patch.object(connect, "_ensure_auth"):
                    client = VolumeClient.from_env()

        assert client._target == "api.chalk.ai:7443"
        client.close()

    def test_channel_options_are_forwarded(self):
        client = VolumeClient(
            "localhost:50051",
            credentials=grpc.ssl_channel_credentials(),
            options=[
                ("grpc.max_send_message_length", -1),
                ("grpc.max_receive_message_length", -1),
            ],
        )

        assert client._target == "localhost:50051"
        client.close()
