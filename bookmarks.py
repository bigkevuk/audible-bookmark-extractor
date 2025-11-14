import json
import os
import re

import audible
from pydub import AudioSegment

from config import END_POSITION_OFFSET, START_POSITION_OFFSET


class BookmarkMixin:
    async def cmd_get_bookmarks(self):
        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return

        for book in li_books:
            print(self.get_bookmarks(book))

    def get_bookmarks(self, book):
        asin = book.get("asin")
        _title = self._get_display_title(book)
        if not _title:
            return

        title_slug, title_dir_path = self._ensure_local_book_path(book)
        title = title_slug

        bookmarks_dir = os.path.join(title_dir_path, "bookmarks")
        os.makedirs(bookmarks_dir, exist_ok=True)
        bookmarks_path = os.path.join(bookmarks_dir, "bookmarks.json")

        li_bookmarks = self._load_bookmarks_from_disk(bookmarks_path)
        if li_bookmarks is None:
            if not asin:
                print(f"No ASIN cached for '{_title}'. Run list_books to refresh the library cache before fetching bookmarks.")
                return
            bookmarks_url = f"https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/sidecar?type=AUDI&key={asin}"
            print(f"Fetching bookmarks from Audible for {_title}")
            try:
                with audible.Client(auth=self.auth, response_callback=self.bookmark_response_callback) as client:
                    library = client.get(
                        bookmarks_url,
                        num_results=1000,
                        response_groups="product_desc, product_attrs"
                    )
                    li_bookmarks = library.json().get("payload", {}).get("records", [])
            except Exception as exc:
                print(f"Failed to retrieve bookmarks from Audible: {exc}")
                return

            self._save_bookmarks_to_disk(bookmarks_path, li_bookmarks)
        else:
            print(f"Loaded cached bookmarks from {bookmarks_path}")

        sorted_records = sorted(li_bookmarks, key=lambda i: i.get("type", ""), reverse=True)
        combined_clips = self._combine_overlapping_bookmarks(li_bookmarks)
        total_clip_entries = len(combined_clips)
        if not total_clip_entries:
            print("No clips found in the bookmark payload.")
            return
        print(f"Preparing {total_clip_entries} clips for {_title}...")

        title_mp3_path = os.path.join(title_dir_path, f"{title}.mp3")
        audio_book = AudioSegment.from_mp3(title_mp3_path)
        chapter_timestamps = self._load_chapter_timestamps(title_dir_path)

        file_counter = 1
        processed_clips = 0
        notes_dict = {}
        clip_metadata_records = []

        clips_dir_path = os.path.join(title_dir_path, "clips")
        if not os.path.exists(clips_dir_path):
            os.makedirs(clips_dir_path)

        for audio_clip in sorted_records:
            raw_start_raw = audio_clip.get("startPosition")
            if raw_start_raw is None:
                continue
            raw_start_pos = int(raw_start_raw)

            if audio_clip.get("type", None) in ["audible.note"]:
                notes_dict[raw_start_pos] = audio_clip.get("text")
                print(f"CLIP: {notes_dict[raw_start_pos]}  {raw_start_pos}")

        for clip_entry in combined_clips:
            raw_start_pos = clip_entry.get("startPosition")
            if raw_start_pos is None:
                continue
            start_pos = raw_start_pos - START_POSITION_OFFSET
            end_pos = int(clip_entry.get("endPosition", raw_start_pos + 30000)) + END_POSITION_OFFSET
            if start_pos == end_pos:
                end_pos += 30000

            clip = audio_book[start_pos:end_pos]

            note_name = self._resolve_clip_note(notes_dict, clip_entry)
            chapter_match = self._resolve_chapter_for_timestamp(chapter_timestamps, raw_start_pos)
            chapter_title = chapter_match.get("title") if chapter_match else None

            base_name = note_name if note_name else f"clip{file_counter:04d}"
            file_name = self._format_clip_filename(base_name, chapter_title)

            clip_path = os.path.join(clips_dir_path, f"{file_name}.flac")
            processed_clips += 1
            print(f"[{processed_clips}/{total_clip_entries}] Exporting {file_name}.flac")
            clip.export(clip_path, format="flac")
            file_counter += 1

            metadata_entry = {
                "file_name": file_name,
                "startPosition": raw_start_pos,
                "endPosition": clip_entry.get("endPosition"),
                "note": note_name
            }
            if chapter_match:
                metadata_entry["chapterTitle"] = chapter_title
                metadata_entry["chapterIndex"] = chapter_match.get("index")
            clip_metadata_records.append(metadata_entry)

        self._save_clip_metadata(clips_dir_path, clip_metadata_records)

    def bookmark_response_callback(self, resp):
        return resp

    def _load_bookmarks_from_disk(self, bookmarks_path):
        if not os.path.exists(bookmarks_path):
            return None
        try:
            with open(bookmarks_path, "r", encoding="utf-8") as bookmarks_file:
                return json.load(bookmarks_file)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Failed to load cached bookmarks ({exc}), refetching...")
            return None

    def _save_bookmarks_to_disk(self, bookmarks_path, li_bookmarks):
        try:
            with open(bookmarks_path, "w", encoding="utf-8") as bookmarks_file:
                json.dump(li_bookmarks, bookmarks_file, indent=2, ensure_ascii=False)
            print(f"Saved bookmark metadata to {bookmarks_path}")
        except OSError as exc:
            print(f"Failed to write bookmark metadata: {exc}")

    def _save_clip_metadata(self, clips_dir_path, metadata):
        metadata_path = os.path.join(clips_dir_path, "clip_metadata.json")
        try:
            with open(metadata_path, "w", encoding="utf-8") as meta_file:
                json.dump(metadata, meta_file, indent=2, ensure_ascii=False)
            print(f"Saved clip metadata to {metadata_path}")
        except OSError as exc:
            print(f"Failed to write clip metadata: {exc}")

    def _combine_overlapping_bookmarks(self, li_bookmarks):
        clip_types = {"audible.clip", "audible.bookmark"}
        clip_entries = []
        for record in li_bookmarks:
            if record.get("type") in clip_types:
                start_raw = record.get("startPosition")
                end_raw = record.get("endPosition")
                if start_raw is None or end_raw is None:
                    continue
                copy_record = record.copy()
                start = int(start_raw)
                end = int(end_raw)
                copy_record["startPosition"] = start
                copy_record["endPosition"] = end
                copy_record["_note_positions"] = [start]
                clip_entries.append(copy_record)

        clip_entries.sort(key=lambda rec: rec["startPosition"])
        merged = []
        for clip in clip_entries:
            if not merged:
                merged.append(clip)
                continue
            last = merged[-1]
            if clip["startPosition"] <= last["endPosition"]:
                last["endPosition"] = max(last["endPosition"], clip["endPosition"])
                last["_note_positions"].extend(clip["_note_positions"])
            else:
                merged.append(clip)
        for clip in merged:
            clip["clip_length_seconds"] = max(clip["endPosition"] - clip["startPosition"], 0) / 1000.0
        return merged

    def _resolve_clip_note(self, notes_dict, clip_entry):
        for pos in clip_entry.get("_note_positions", []):
            if pos in notes_dict:
                return notes_dict[pos]
        start_pos = clip_entry.get("startPosition")
        if start_pos in notes_dict:
            return notes_dict[start_pos]
        return None

    def _format_clip_filename(self, base_name, chapter_title):
        if not chapter_title:
            return base_name
        chapter_component = self._sanitize_filename_component(chapter_title)
        if not chapter_component:
            return base_name
        return f"{base_name}__{chapter_component}"

    def _sanitize_filename_component(self, value):
        if not value:
            return ""
        value = value.strip()
        if not value:
            return ""
        sanitized = re.sub(r"[\\/:*?\"<>|]", "", value)
        sanitized = re.sub(r"\s+", "_", sanitized)
        sanitized = sanitized.strip("._")
        return sanitized or "chapter"
