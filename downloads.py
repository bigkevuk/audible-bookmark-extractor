import asyncio
import json
import os
import sys
from datetime import datetime

import audible
import requests

from constants import artifacts_root_directory
from errors import ExternalError

from config import AUDIBLE_URL_BASE, country_code_mapping


class DownloadMixin:
    async def cmd_download_books(self):
        li_books = await self.get_book_selection(source="library")
        if not li_books:
            return

        tasks = [
            asyncio.ensure_future(self.get_book_infos(book.get("asin")))
            for book in li_books
        ]
        books = await asyncio.gather(*tasks)

        for book in books:
            if book is None:
                continue
            print(book["item"]["title"])
            asin = book["item"]["asin"]
            raw_title = book["item"]["title"]
            title = raw_title.lower().replace(" ", "_")

            try:
                download_url = self.get_download_url(
                    self.generate_url(self.auth.locale.country_code, "download", asin),
                    num_results=1000,
                    response_groups="product_desc, product_attrs"
                )
            except audible.exceptions.NetworkError as exc:
                ExternalError(self.get_download_url, asin, exc).show_error()
                continue

            audible_response = requests.get(download_url, stream=True)
            title_dir_path = os.path.join(artifacts_root_directory, "audiobooks", title)
            os.makedirs(title_dir_path, exist_ok=True)

            chapter_info_payload = (book.get("item") or {}).get("chapter_info") if hasattr(book, "get") else None
            if not chapter_info_payload:
                chapter_info_payload = self._fetch_chapter_info(asin)

            self._persist_chapter_timestamps(book, title_dir_path, explicit_chapter_info=chapter_info_payload)

            if audible_response.ok:
                title_file_path = os.path.join(title_dir_path, f"{title}.aax")
                with open(title_file_path, 'wb') as f:
                    print("Downloading %s" % raw_title)

                    total_length = audible_response.headers.get('content-length')

                    if total_length is None:
                        print("Unable to estimate download size, downloading, this might take a while...")
                        f.write(audible_response.content)
                    else:
                        dl = 0
                        total_length = int(total_length)
                        for data in audible_response.iter_content(chunk_size=1024 * 1024):
                            dl += len(data)
                            f.write(data)
                            done = int(50 * dl / total_length)
                            sys.stdout.write("\r[%s%s]" % ('=' * done, ' ' * (50 - done)))
                            sys.stdout.write(f"   {int(dl / total_length * 100)}%")
                            sys.stdout.flush()
            else:
                print(audible_response.text)

    def generate_url(self, country_code, url_type, asin=None):
        if asin and url_type == "download":
            locale_suffix = country_code_mapping.get(country_code)
            return f"{AUDIBLE_URL_BASE}{locale_suffix}/library/download?asin={asin}&codec=AAX"
        return None

    def get_download_link_callback(self, resp):
        return resp.next_request

    def get_download_url(self, url, **kwargs):
        with audible.Client(auth=self.auth, response_callback=self.get_download_link_callback) as client:
            library = client.get(url, **kwargs)
            return library.url

    async def cmd_convert_audiobook(self):
        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return

        for book in li_books:
            _title = self._get_display_title(book)
            if not _title:
                return

            title, title_dir_path = self._ensure_local_book_path(book)
            activation_bytes = self.get_activation_bytes()
            title_aax_path = os.path.join(title_dir_path, f"{title}.aax")
            title_m4b_path = os.path.join(title_dir_path, f"{title}.m4b")
            title_mp3_path = os.path.join(title_dir_path, f"{title}.mp3")
            os.system(
                f"ffmpeg -activation_bytes {activation_bytes} -i {title_aax_path} -c copy {title_m4b_path}"
            )
            os.system(f"ffmpeg -i {title_m4b_path} {title_mp3_path}")

    def get_activation_bytes(self):
        activation_bytes_path = os.path.join(artifacts_root_directory, "secrets", "activation_bytes.txt")
        if os.path.exists(activation_bytes_path):
            with open(activation_bytes_path) as f:
                activation_bytes = f.readlines()[0]
        else:
            activation_bytes = self.auth.get_activation_bytes(activation_bytes_path, True)
            with open(activation_bytes_path, "w") as text_file:
                text_file.write(activation_bytes)
        return activation_bytes

    def _fetch_chapter_info(self, asin):
        if not asin:
            return None
        request_body = {
            "supported_drm_types": ["Mpeg", "Adrm"],
            "quality": "Normal",
            "consumption_type": "Download",
            "response_groups": "chapter_info"
        }
        try:
            with audible.Client(auth=self.auth) as client:
                response = client.post(f"/1.0/content/{asin}/licenserequest", request_body)
        except Exception as exc:
            print(f"Unable to fetch chapter metadata for {asin}: {exc}")
            return None

        content_metadata = ((response or {}).get("content_license") or {}).get("content_metadata") or {}
        return content_metadata.get("chapter_info")

    def _persist_chapter_timestamps(self, book_payload, title_dir_path, explicit_chapter_info=None):
        if not isinstance(book_payload, dict):
            return
        item = book_payload.get("item") or {}
        chapter_info = explicit_chapter_info or item.get("chapter_info")
        chapters = self._normalize_chapter_timestamps(chapter_info)
        if not chapters:
            return

        payload = {
            "asin": item.get("asin"),
            "retrieved_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "chapters": chapters
        }

        chapters_path = os.path.join(title_dir_path, "chapter_timestamps.json")
        try:
            with open(chapters_path, "w", encoding="utf-8") as chapter_file:
                json.dump(payload, chapter_file, indent=2, ensure_ascii=False)
            print(f"Saved {len(chapters)} chapter timestamps to {chapters_path}")
        except OSError as exc:
            print(f"Failed to write chapter timestamps: {exc}")

    def _load_chapter_timestamps(self, title_dir_path):
        chapters_path = os.path.join(title_dir_path, "chapter_timestamps.json")
        if not os.path.exists(chapters_path):
            return None
        try:
            with open(chapters_path, "r", encoding="utf-8") as chapter_file:
                return json.load(chapter_file)
        except (OSError, json.JSONDecodeError):
            return None

    def _normalize_chapter_timestamps(self, chapter_info):
        if not isinstance(chapter_info, dict):
            return None
        raw_chapters = chapter_info.get("chapters")
        if not isinstance(raw_chapters, list):
            return None

        normalized = []
        for index, chapter in enumerate(raw_chapters, start=1):
            if not isinstance(chapter, dict):
                continue
            start_ms = self._coerce_int(
                chapter.get("start_offset_ms")
                or chapter.get("start_offset")
                or chapter.get("startOffsetMs")
                or chapter.get("startPosition")
                or chapter.get("start_position_ms")
            )
            end_ms = self._coerce_int(
                chapter.get("end_offset_ms")
                or chapter.get("end_offset")
                or chapter.get("endOffsetMs")
                or chapter.get("endPosition")
                or chapter.get("end_position_ms")
            )
            length_ms = self._coerce_int(
                chapter.get("length_ms")
                or chapter.get("length")
                or chapter.get("duration_ms")
                or chapter.get("chapter_length_ms")
            )
            if end_ms is None and start_ms is not None and length_ms is not None:
                end_ms = start_ms + length_ms

            normalized.append({
                "index": index,
                "title": chapter.get("title") or f"Chapter {index}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "length_ms": length_ms
            })

        return normalized or None

    def _coerce_int(self, value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None

    def _resolve_chapter_for_timestamp(self, chapter_payload, position_ms):
        if chapter_payload is None:
            return None
        try:
            position = int(position_ms)
        except (TypeError, ValueError):
            return None

        if isinstance(chapter_payload, dict):
            chapters = chapter_payload.get("chapters")
        elif isinstance(chapter_payload, list):
            chapters = chapter_payload
        else:
            return None

        if not isinstance(chapters, list):
            return None

        best_match = None
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            start_ms = self._coerce_int(
                chapter.get("start_ms")
                or chapter.get("startPosition")
                or chapter.get("start_offset_ms")
            )
            if start_ms is None:
                continue
            end_ms = self._coerce_int(
                chapter.get("end_ms")
                or chapter.get("endPosition")
                or chapter.get("end_offset_ms")
            )

            best_match = chapter
            if end_ms is not None and position < end_ms:
                return chapter

        return best_match
