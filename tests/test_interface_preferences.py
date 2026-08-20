import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from gamdl_cn.api.itunes import ItunesApi
from gamdl_cn.cli.utils import CustomOutputWriter
from gamdl_cn.interface.interface import AppleMusicInterface


class InterfaceUrlTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _interface() -> AppleMusicInterface:
        apple_music_api = SimpleNamespace(
            storefront="us",
            language="en-US",
            client=SimpleNamespace(
                headers={"accept-language": "en-US"},
                params=httpx.QueryParams({"l": "en-US"}),
            ),
        )
        itunes_api = SimpleNamespace(
            set_storefront=AsyncMock(),
            set_language=Mock(),
        )
        base = SimpleNamespace(
            apple_music_api=apple_music_api,
            itunes_api=itunes_api,
        )
        return AppleMusicInterface(
            song=SimpleNamespace(base=base),
            music_video=SimpleNamespace(),
            uploaded_video=SimpleNamespace(),
        )

    def test_album_song_id_is_independent_of_query_parameter_order(self) -> None:
        urls = (
            "https://music.apple.com/us/album/example/123?i=456&l=zh-Hans-CN",
            "https://music.apple.com/us/album/example/123?l=zh-Hans-CN&i=456",
        )
        self.assertEqual(
            [AppleMusicInterface.get_url_info(url).sub_id for url in urls],
            ["456", "456"],
        )

    def test_url_parser_rejects_untrusted_or_partial_origins(self) -> None:
        self.assertIsNone(
            AppleMusicInterface.get_url_info(
                "https://music.apple.com.example/us/song/example/123"
            )
        )
        self.assertIsNone(
            AppleMusicInterface.get_url_info(
                "https://music.apple.com/us/song/example/123/trailing"
            )
        )

    async def test_url_preferences_restore_defaults_between_urls(self) -> None:
        interface = self._interface()
        localized_url = (
            "https://music.apple.com/cn/song/example/123?l=zh-Hans-CN"
        )
        default_url = "https://music.apple.com/us/song/example/456"

        await interface._apply_url_preferences(
            localized_url,
            interface.get_url_info(localized_url),
        )
        await interface._apply_url_preferences(
            default_url,
            interface.get_url_info(default_url),
        )

        apple_music_api = interface.base.apple_music_api
        self.assertEqual(apple_music_api.storefront, "us")
        self.assertEqual(apple_music_api.language, "en-US")
        self.assertEqual(apple_music_api.client.headers["accept-language"], "en-US")
        self.assertEqual(apple_music_api.client.params["l"], "en-US")
        self.assertEqual(
            interface.base.itunes_api.set_storefront.await_args_list[0].args,
            ("cn",),
        )
        self.assertEqual(
            interface.base.itunes_api.set_storefront.await_args_list[1].args,
            ("us",),
        )

    async def test_itunes_storefront_update_is_atomic(self) -> None:
        api = ItunesApi.__new__(ItunesApi)
        api.storefront = "us"
        api.storefront_id = 143441
        with patch.object(
            api,
            "get_storefront_id",
            AsyncMock(side_effect=RuntimeError("lookup failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "lookup failed"):
                await api.set_storefront("cn")

        self.assertEqual(api.storefront, "us")
        self.assertEqual(api.storefront_id, 143441)


class CliUtilityTests(unittest.TestCase):
    def test_output_writer_does_not_share_default_stream_list(self) -> None:
        first = CustomOutputWriter()
        second = CustomOutputWriter()
        first.streams.append(io.StringIO())
        self.assertEqual(len(second.streams), 1)


if __name__ == "__main__":
    unittest.main()
