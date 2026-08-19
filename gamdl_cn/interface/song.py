import asyncio
import datetime
import re
from typing import AsyncGenerator, Callable
from xml.dom import minidom
from xml.etree import ElementTree

import m3u8
import structlog

from ..api.exceptions import GamdlApiResponseError
from .base import AppleMusicBaseInterface
from .constants import DRM_DEFAULT_KEY_MAPPING, MP4_FORMAT_CODECS, SONG_CODEC_REGEX_MAP
from .enums import SongCodec, SyncedLyricsFormat
from .exceptions import (
    GamdlInterfaceDecryptionNotAvailableError,
    GamdlInterfaceFormatNotAvailableError,
    GamdlInterfaceMediaNotStreamableError,
)
from .types import (
    AppleMusicMedia,
    DecryptionKeyAv,
    Lyrics,
    MediaFileFormat,
    StreamInfo,
    StreamInfoAv,
)

logger = structlog.get_logger(__name__)
TTML_NS = "http://www.w3.org/ns/ttml"
ITUNES_LYRICS_NS = "http://music.apple.com/lyric-ttml-internal"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class AppleMusicSongInterface:
    def __init__(
        self,
        base: AppleMusicBaseInterface,
        synced_lyrics_format: SyncedLyricsFormat = SyncedLyricsFormat.LRC,
        codec_priority: list[SongCodec] = [SongCodec.AAC_WEB],
        use_album_date: bool = False,
        skip_stream_info: bool = False,
        ask_codec_function: Callable[[list[dict]], dict | None] | None = None,
    ):
        self.base = base
        self.synced_lyrics_format = synced_lyrics_format
        self.codec_priority = codec_priority
        self.use_album_date = use_album_date
        self.skip_stream_info = skip_stream_info
        self.ask_codec_function = ask_codec_function

    async def get_lyrics(
        self,
        song_metadata: dict,
    ) -> Lyrics | None:
        log = logger.bind(
            action="get_lyrics",
            song_id=song_metadata["id"],
        )

        if song_metadata["attributes"]["playParams"].get("isLibrary"):
            log.debug("library_song_no_lyrics")
            return None

        if not song_metadata["attributes"]["hasLyrics"]:
            log.debug("no_lyrics")
            return None

        lyrics_ttml = await self._get_lyrics_ttml(song_metadata["id"])
        if not lyrics_ttml:
            log.debug("no_lyrics_data")
            return None

        lyrics = self._get_lyrics(lyrics_ttml)

        log.debug("success", lyrics=lyrics)

        return lyrics

    async def _get_lyrics_ttml(self, song_id: str) -> str | None:
        lyrics_ttml = None

        try:
            syllable_lyrics = await self.base.apple_music_api.get_syllable_lyrics(
                song_id
            )
        except GamdlApiResponseError as exc:
            logger.debug(
                "failed_to_fetch_syllable_lyrics",
                song_id=song_id,
                error=str(exc),
            )
        else:
            lyrics_ttml = self._get_lyrics_ttml_from_api_response(syllable_lyrics)

        if lyrics_ttml:
            return lyrics_ttml

        song = (
            await self.base.apple_music_api.get_song(
                song_id,
                extend="ttmlLocalizations",
            )
        )["data"][0]
        return self._get_lyrics_ttml_from_song(song)

    def _get_lyrics_ttml_from_song(self, song_metadata: dict) -> str | None:
        lyrics_data = song_metadata.get("relationships", {}).get("lyrics", {}).get(
            "data",
            [],
        )
        for lyric_resource in lyrics_data:
            lyrics_ttml = self._get_lyrics_ttml_from_attributes(
                lyric_resource.get("attributes", {})
            )
            if lyrics_ttml:
                return lyrics_ttml
        return None

    def _get_lyrics_ttml_from_api_response(self, api_response: dict) -> str | None:
        for lyric_resource in api_response.get("data", []):
            lyrics_ttml = self._get_lyrics_ttml_from_attributes(
                lyric_resource.get("attributes", {})
            )
            if lyrics_ttml:
                return lyrics_ttml
        return None

    def _get_lyrics_ttml_from_attributes(self, attributes: dict) -> str | None:
        direct_ttml = attributes.get("ttml")
        if direct_ttml:
            return direct_ttml

        localized_ttml_direct = attributes.get("ttmlLocalizations")
        if isinstance(localized_ttml_direct, str) and localized_ttml_direct.strip():
            return localized_ttml_direct

        localized_ttml = self._get_localized_lyrics_ttml(localized_ttml_direct)
        if localized_ttml:
            return localized_ttml

        return None

    def _get_localized_lyrics_ttml(self, ttml_localizations) -> str | None:
        localizations = self._flatten_ttml_localizations(ttml_localizations)
        if not localizations:
            return None

        target_language_tag = self.base.apple_music_api.get_lyrics_language_tag()
        target_script_tag = self.base.apple_music_api.get_lyrics_script_tag()

        for target in (target_language_tag, target_script_tag):
            if not target:
                continue

            target_lower = target.lower()
            for localization in localizations:
                localization_tag = self._get_localization_language_tag(localization)
                if localization_tag and localization_tag.lower() == target_lower:
                    lyrics_ttml = self._extract_ttml_from_localization(localization)
                    if lyrics_ttml:
                        return lyrics_ttml

        for localization in localizations:
            localization_tag = self._get_localization_language_tag(localization)
            if (
                target_script_tag
                and localization_tag
                and localization_tag.lower().startswith(target_script_tag.lower())
            ):
                lyrics_ttml = self._extract_ttml_from_localization(localization)
                if lyrics_ttml:
                    return lyrics_ttml

        for localization in localizations:
            lyrics_ttml = self._extract_ttml_from_localization(localization)
            if lyrics_ttml:
                return lyrics_ttml

        return None

    def _flatten_ttml_localizations(self, ttml_localizations) -> list[dict]:
        if isinstance(ttml_localizations, str):
            return []
        if isinstance(ttml_localizations, list):
            return [i for i in ttml_localizations if isinstance(i, dict)]
        if isinstance(ttml_localizations, dict):
            return [i for i in ttml_localizations.values() if isinstance(i, dict)]
        return []

    def _get_localization_language_tag(self, localization: dict) -> str | None:
        for key in ("language", "locale", "tag"):
            value = localization.get(key)
            if isinstance(value, str):
                return value

        attributes = localization.get("attributes")
        if isinstance(attributes, dict):
            for key in ("language", "locale", "tag"):
                value = attributes.get(key)
                if isinstance(value, str):
                    return value

        return None

    def _extract_ttml_from_localization(self, localization: dict) -> str | None:
        lyrics_ttml = localization.get("ttml")
        if isinstance(lyrics_ttml, str):
            return lyrics_ttml

        attributes = localization.get("attributes")
        if isinstance(attributes, dict):
            lyrics_ttml = attributes.get("ttml")
            if isinstance(lyrics_ttml, str):
                return lyrics_ttml

        return None

    def _get_lyrics(
        self,
        lyrics_ttml: str,
    ) -> Lyrics:
        lyrics_ttml_et = ElementTree.fromstring(lyrics_ttml)
        replacement_text_by_key = self._get_translation_replacement_texts(lyrics_ttml_et)
        unsynced_lyrics = []
        synced_lyrics = []
        index = 1

        for div in lyrics_ttml_et.iter(f"{{{TTML_NS}}}div"):
            stanza = []
            unsynced_lyrics.append(stanza)

            for p in div.iter(f"{{{TTML_NS}}}p"):
                text = self._get_lyrics_text(p, replacement_text_by_key)
                if text:
                    stanza.append(text)

                if p.attrib.get("begin"):
                    if self.synced_lyrics_format == SyncedLyricsFormat.LRC:
                        synced_lyrics.append(
                            self._get_lyrics_line_lrc(p, replacement_text_by_key)
                        )

                    if self.synced_lyrics_format == SyncedLyricsFormat.SRT:
                        synced_lyrics.append(
                            self._get_lyrics_line_srt(index, p, replacement_text_by_key)
                        )

                    if self.synced_lyrics_format == SyncedLyricsFormat.TTML:
                        if not synced_lyrics:
                            synced_lyrics.append(
                                minidom.parseString(lyrics_ttml).toprettyxml()
                            )
                        continue

                    index += 1

        return Lyrics(
            synced="\n".join(synced_lyrics + ["\n"]) if synced_lyrics else None,
            unsynced=(
                "\n\n".join(["\n".join(lyric_group) for lyric_group in unsynced_lyrics])
                if unsynced_lyrics
                else None
            ),
        )

    def _parse_ttml_timestamp(
        self,
        timestamp_ttml: str,
    ) -> datetime.datetime:
        mins_secs_ms = re.findall(r"\d+", timestamp_ttml)
        ms, secs, mins = 0, 0, 0

        if len(mins_secs_ms) == 2 and ":" in timestamp_ttml:
            secs, mins = int(mins_secs_ms[-1]), int(mins_secs_ms[-2])

        elif len(mins_secs_ms) == 1:
            ms = int(mins_secs_ms[-1])

        else:
            secs = float(f"{mins_secs_ms[-2]}.{mins_secs_ms[-1]}")
            if len(mins_secs_ms) > 2:
                mins = int(mins_secs_ms[-3])

        return datetime.datetime.fromtimestamp(
            (mins * 60) + secs + (ms / 1000),
            tz=datetime.timezone.utc,
        )

    def _get_lyrics_line_srt(
        self,
        index: int,
        element: ElementTree.Element,
        replacement_text_by_key: dict[str, str] | None = None,
    ) -> str:
        timestamp_begin_ttml = element.attrib.get("begin")
        timestamp_end_ttml = element.attrib.get("end")
        text = self._get_lyrics_text(element, replacement_text_by_key)

        timestamp_begin = self._parse_ttml_timestamp(timestamp_begin_ttml)
        timestamp_end = self._parse_ttml_timestamp(timestamp_end_ttml)

        return (
            f"{index}\n"
            f"{timestamp_begin.strftime('%H:%M:%S,%f')[:-3]} --> "
            f"{timestamp_end.strftime('%H:%M:%S,%f')[:-3]}\n"
            f"{text}\n"
        )

    def _get_lyrics_line_lrc(
        self,
        element: ElementTree.Element,
        replacement_text_by_key: dict[str, str] | None = None,
    ) -> str:
        timestamp_ttml = element.attrib.get("begin")
        text = self._get_lyrics_text(element, replacement_text_by_key)

        timestamp = self._parse_ttml_timestamp(timestamp_ttml)
        ms_new = timestamp.strftime("%f")[:-3]

        if int(ms_new[-1]) >= 5:
            ms = int(f"{int(ms_new[:2]) + 1}") * 10
            timestamp += datetime.timedelta(milliseconds=ms) - datetime.timedelta(
                microseconds=timestamp.microsecond
            )

        return f"[{timestamp.strftime('%M:%S.%f')[:-4]}]{text}"

    def _get_lyrics_text(
        self,
        element: ElementTree.Element,
        replacement_text_by_key: dict[str, str] | None = None,
    ) -> str:
        if replacement_text_by_key:
            lyrics_key = self._get_lyrics_line_key(element)
            replacement_text = replacement_text_by_key.get(lyrics_key)
            if replacement_text:
                return replacement_text

        text = "".join(element.itertext()).strip()
        return re.sub(r"\s+", " ", text)

    def _get_translation_replacement_texts(
        self,
        lyrics_ttml_et: ElementTree.Element,
    ) -> dict[str, str]:
        target_language_tag = self.base.apple_music_api.get_lyrics_language_tag()
        target_script_tag = self.base.apple_music_api.get_lyrics_script_tag()
        replacement_text_by_key = {}

        for translation in lyrics_ttml_et.iterfind(
            f".//{{{ITUNES_LYRICS_NS}}}translation"
        ):
            if translation.attrib.get("type") != "replacement":
                continue

            translation_language = translation.attrib.get(f"{{{XML_NS}}}lang")
            if not self._translation_matches_target_language(
                translation_language,
                target_language_tag,
                target_script_tag,
            ):
                continue

            for text_element in translation.iterfind(f"{{{ITUNES_LYRICS_NS}}}text"):
                lyrics_key = text_element.attrib.get("for")
                if not lyrics_key:
                    continue

                replacement_text = "".join(text_element.itertext()).strip()
                replacement_text = re.sub(r"\s+", " ", replacement_text)
                if replacement_text:
                    replacement_text_by_key[lyrics_key] = replacement_text

        return replacement_text_by_key

    def _translation_matches_target_language(
        self,
        translation_language: str | None,
        target_language_tag: str | None,
        target_script_tag: str | None,
    ) -> bool:
        if not translation_language:
            return False

        translation_language_lower = translation_language.lower()
        for target in (target_language_tag, target_script_tag):
            if target and translation_language_lower == target.lower():
                return True

        if target_script_tag and translation_language_lower.startswith(
            target_script_tag.lower()
        ):
            return True

        return False

    def _get_lyrics_line_key(self, element: ElementTree.Element) -> str | None:
        return element.attrib.get(f"{{{ITUNES_LYRICS_NS}}}key")

    def _switch_m3u8_master_url_to_default(self, m3u8_master_url: str) -> str:
        return re.sub(
            r"(P\d+)_[^/]+(\.m3u8)",
            r"\1_default\2",
            m3u8_master_url,
        )

    def _get_m3u8_from_playback(self, playback: dict) -> str | None:
        log = logger.bind(action="get_m3u8_master_url_from_playback")

        m3u8_master_url = playback["songList"][0].get("hls-playlist-url")

        if m3u8_master_url:
            m3u8_master_url = self._switch_m3u8_master_url_to_default(m3u8_master_url)
            log.debug("success", m3u8_master_url=m3u8_master_url)
            return m3u8_master_url

        log.debug("no_m3u8_master_url")

    async def _get_m3u8_master_url_from_assets(
        self,
        media_id: str,
    ) -> str | None:
        log = logger.bind(
            action="get_m3u8_master_url_from_assets",
            song_id=media_id,
        )

        assets = await self.base.apple_music_api.get_assets(
            media_id,
            "song",
        )

        asset = next(
            (
                asset
                for asset in assets.get("results", {}).get("assets", [])
                if asset.get("url")
            ),
            None,
        )
        enhanced = asset["url"] if asset else None

        if enhanced:
            enhanced = self._switch_m3u8_master_url_to_default(enhanced)
            log.debug("success", m3u8_master_url=enhanced)
            return enhanced

        log.debug("no_m3u8_master_url")

        return None

    async def _get_m3u8_master_url(
        self,
        media_id: str,
        playback: dict | None,
    ) -> str | None:
        if playback:
            m3u8_master_url = self._get_m3u8_from_playback(playback)
            if m3u8_master_url:
                return m3u8_master_url

        return await self._get_m3u8_master_url_from_assets(media_id)

    async def get_stream_info(
        self,
        media_id: str,
        is_library: bool,
        webplayback: dict | None = None,
        playback: dict | None = None,
    ) -> StreamInfoAv:
        stream_info = None

        if is_library:
            stream_info = await self._get_library_stream_info(webplayback)
        else:
            m3u8_master_url = None
            fetched_m3u8_master_url = False

            for codec in self.codec_priority:
                if codec.is_web:
                    stream_info = await self._get_web_stream_info(webplayback, codec)
                else:
                    if not fetched_m3u8_master_url:
                        m3u8_master_url = await self._get_m3u8_master_url(
                            media_id,
                            playback,
                        )
                        fetched_m3u8_master_url = True

                    stream_info = await self._get_stream_info_nonweb(
                        m3u8_master_url,
                        codec,
                    )

                if stream_info:
                    break

        if not stream_info:
            raise GamdlInterfaceFormatNotAvailableError(
                media_id=media_id,
                codec=[codec.value for codec in self.codec_priority],
            )

        return stream_info

    async def _get_stream_info_nonweb(
        self,
        m3u8_master_url: str | None,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info")

        if not m3u8_master_url:
            log.debug("no_m3u8_master_url")
            return None

        m3u8_master_obj = m3u8.loads(
            (await self.base.get_response(m3u8_master_url)).text
        )
        m3u8_master_data = m3u8_master_obj.data
        is_enhanced = self._is_enhanced_m3u8_master(m3u8_master_data)

        if is_enhanced:
            stream_info = await self._get_stream_info_enhanced(
                m3u8_master_url,
                m3u8_master_data,
                codec,
            )
        else:
            stream_info = await self._get_stream_info_nonenhanced(
                m3u8_master_url,
                m3u8_master_data,
                codec,
            )

        if stream_info:
            log.debug(
                "success",
                stream_info=stream_info,
                is_enhanced=is_enhanced,
            )

        return stream_info

    def _is_enhanced_m3u8_master(self, m3u8_master_data: dict) -> bool:
        return any(
            playlist.get("stream_info", {}).get("audio")
            for playlist in m3u8_master_data.get("playlists", [])
        )

    async def _get_stream_info_enhanced(
        self,
        m3u8_master_url: str,
        m3u8_master_data: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info_enhanced")

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec_enhanced(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = await self._get_stream_info_from_playlist(
            m3u8_master_url,
            playlist,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    async def _get_stream_info_nonenhanced(
        self,
        m3u8_master_url: str,
        m3u8_master_data: dict,
        codec: SongCodec,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_song_stream_info_nonenhanced")

        if codec == SongCodec.ASK:
            playlist = await self._get_playlist_from_user(m3u8_master_data)
        else:
            playlist = self._get_playlist_from_codec_nonenhanced(
                m3u8_master_data,
                codec,
            )

        if playlist is None:
            log.debug("no_matching_playlist", codec=codec.value)
            return None

        stream_info = await self._get_stream_info_from_playlist(
            m3u8_master_url,
            playlist,
            True,
        )

        log.debug("success", stream_info=stream_info)

        return stream_info

    async def _get_stream_info_from_playlist(
        self,
        m3u8_master_url: str,
        playlist: dict,
        use_single_content_key: bool = False,
    ) -> StreamInfoAv:
        stream_info = StreamInfo(use_single_content_key=use_single_content_key)
        stream_info.stream_url = (
            f"{m3u8_master_url.rpartition('/')[0]}/{playlist['uri']}"
        )
        stream_info.codec = playlist["stream_info"]["codecs"]
        is_mp4 = any(stream_info.codec.startswith(codec) for codec in MP4_FORMAT_CODECS)

        m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )

        stream_info.widevine_pssh = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
        )
        stream_info.playready_pssh = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "com.microsoft.playready",
        )
        stream_info.fairplay_key = self._get_drm_uri_from_m3u8_keys(
            m3u8_obj,
            "com.apple.streamingkeydelivery",
        )

        stream_info_av = StreamInfoAv(
            audio_track=stream_info,
            file_format=MediaFileFormat.MP4 if is_mp4 else MediaFileFormat.M4A,
        )

        return stream_info_av

    def _get_playlist_from_codec_enhanced(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if re.fullmatch(
                SONG_CODEC_REGEX_MAP[codec.value], playlist["stream_info"]["audio"]
            )
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    def _get_playlist_from_codec_nonenhanced(
        self, m3u8_data: dict, codec: SongCodec
    ) -> dict | None:
        codec_values = {
            SongCodec.AAC: {"mp4a.40.2"},
            SongCodec.AAC_HE: {"mp4a.40.5"},
        }.get(codec)
        if not codec_values:
            return None

        matching_playlists = [
            playlist
            for playlist in m3u8_data["playlists"]
            if playlist["stream_info"].get("codecs") in codec_values
        ]

        if not matching_playlists:
            return None

        return max(
            matching_playlists,
            key=lambda x: x["stream_info"]["average_bandwidth"],
        )

    async def _get_playlist_from_user(self, m3u8_data: dict) -> dict | None:
        if self.ask_codec_function:
            playlist = self.ask_codec_function(
                [playlist for playlist in m3u8_data["playlists"]]
            )
            if asyncio.iscoroutine(playlist):
                playlist = await playlist

            return playlist

        return None

    def _get_drm_uri_from_m3u8_keys(
        self,
        m3u8_obj: m3u8.M3U8,
        drm_key: str,
    ) -> str | None:
        default_uri = DRM_DEFAULT_KEY_MAPPING[drm_key]

        for key in m3u8_obj.keys:
            if key.keyformat == drm_key and key.uri != default_uri:
                return key.uri
        return None

    async def _get_web_stream_info(
        self,
        webplayback: dict | None,
        codec: SongCodec,
    ) -> StreamInfoAv:
        log = logger.bind(action="get_web_song_stream_info")

        if not webplayback:
            log.debug("no_webplayback")
            return None

        flavor = codec.flavor

        stream_info = StreamInfo(
            use_cenc=codec.is_cenc,
        )
        asset = next(
            (i for i in webplayback["songList"][0]["assets"] if i["flavor"] == flavor),
            None,
        )
        if not asset:
            log.debug("no_matching_asset", codec=codec.value, flavor=flavor)
            return None

        stream_info.stream_url = asset["URL"]

        m3u8_obj = m3u8.loads(
            (await self.base.get_response(stream_info.stream_url)).text
        )

        if stream_info.use_cenc:
            stream_info.widevine_pssh = m3u8_obj.keys[0].uri
        else:
            stream_info.fairplay_key = m3u8_obj.keys[0].uri

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    async def _get_library_stream_info(
        self,
        webplayback: dict | None,
    ) -> StreamInfoAv | None:
        log = logger.bind(action="get_library_song_stream_info")

        if not webplayback:
            log.debug("no_webplayback")
            return None

        stream_info = StreamInfo(drm_free=True)

        if len(webplayback["songList"][0]["assets"]) == 0:
            log.debug("no_matching_asset")
            return None
        asset = webplayback["songList"][0]["assets"][0]

        stream_info.stream_url = asset["URL"]

        stream_info_av = StreamInfoAv(
            media_id=webplayback["songList"][0]["songId"],
            audio_track=stream_info,
            file_format=MediaFileFormat.M4A,
        )
        log.debug("success", stream_info=stream_info_av)

        return stream_info_av

    async def get_media(
        self,
        media: AppleMusicMedia,
    ) -> AsyncGenerator[AppleMusicMedia, None]:
        if not media.media_metadata:
            media.media_metadata = (
                await (
                    self.base.apple_music_api.get_library_song(media.media_id)
                    if media.is_library
                    else self.base.apple_music_api.get_song(media.media_id)
                )
            )["data"][0]

        if media.media_metadata["attributes"].get("playParams", {}).get("isLibrary"):
            catalog_metadata = self.base.get_catalog_metadata_from_library(
                media.media_metadata
            )
            if catalog_metadata:
                media.media_id = catalog_metadata["id"]
                media.is_library = False
                media.media_metadata = catalog_metadata

        yield media
        if not self.base.is_media_streamable(media.media_metadata):
            raise GamdlInterfaceMediaNotStreamableError(
                media_id=media.media_id,
            )

        if media.playlist_metadata:
            media.playlist_tags = self.base.get_playlist_tags(
                media.playlist_metadata,
                media.index,
            )

        media.cover = await self.base.get_cover(media.media_metadata)

        media.lyrics = await self.get_lyrics(media.media_metadata)

        if self.base.wrapper_api:
            playback = (
                await self.base.wrapper_api.get_playback(media.media_id)
                if not media.is_library
                else None
            )
            webplayback = (
                await self.base.apple_music_api.get_webplayback(
                    media.media_id,
                    media.is_library,
                )
                if media.is_library
                or any(codec.is_web for codec in self.codec_priority)
                else None
            )
        else:
            playback = None
            webplayback = await self.base.apple_music_api.get_webplayback(
                media.media_id,
                media.is_library,
            )

        if playback:
            media.tags = await self.base.get_tags_from_asset_info(
                playback["songList"][0]["assets"][0]["metadata"],
                media.lyrics.unsynced if media.lyrics else None,
                self.use_album_date,
            )
        else:
            media.tags = await self.base.get_tags_from_asset_info(
                webplayback["songList"][0]["assets"][0]["metadata"],
                media.lyrics.unsynced if media.lyrics else None,
                self.use_album_date,
            )

        if not self.skip_stream_info:
            media.stream_info = await self.get_stream_info(
                media.media_id,
                media.is_library,
                webplayback,
                playback,
            )

            if media.stream_info.audio_track.drm_free:
                pass
            elif (
                not self.base.wrapper_api
                and not media.stream_info.audio_track.widevine_pssh
            ) or (
                self.base.wrapper_api
                and not media.stream_info.audio_track.fairplay_key
                and not media.stream_info.audio_track.use_cenc
            ):
                raise GamdlInterfaceDecryptionNotAvailableError(media_id=media.media_id)
            elif media.stream_info.audio_track.widevine_pssh:
                media.decryption_key = DecryptionKeyAv(
                    audio_track=await self.base.get_decryption_key(
                        media.stream_info.audio_track.widevine_pssh,
                        media.media_id,
                    )
                )

        media.partial = False

        yield media
