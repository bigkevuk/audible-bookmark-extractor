import json
import os
import re

from constants import artifacts_root_directory


class AudibleBase:
    def __init__(self, auth):
        self.auth = auth
        self.books = []
        self.library = {}
        self._pdf_cache = {}
        self._epub_cache = {}
        self._library_cache_path = os.path.join(os.path.dirname(__file__), "library_cache.json")
        self._load_library_cache()

    def _slugify_title(self, title):
        if not title:
            return "untitled"
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        return slug or "untitled"

    def _normalize_title_field(self, title_field):
        if isinstance(title_field, dict):
            return title_field
        if isinstance(title_field, str):
            return {"title": title_field}
        return {"title": "Untitled"}

    def _get_display_title(self, book):
        title_field = book.get("title")
        if isinstance(title_field, dict):
            return title_field.get("title") or title_field.get("name") or "Untitled"
        if isinstance(title_field, str):
            return title_field or "Untitled"
        return "Untitled"

    def _get_downloaded_books(self):
        audiobooks_root = os.path.join(artifacts_root_directory, "audiobooks")
        if not os.path.isdir(audiobooks_root):
            return []

        cache_map = {}
        for item in self.library.get("items", []):
            normalized_title = self._get_display_title(item)
            slug = self._slugify_title(normalized_title)
            cache_map[slug] = item

        downloaded = []
        for entry in sorted(os.listdir(audiobooks_root)):
            entry_path = os.path.join(audiobooks_root, entry)
            if not os.path.isdir(entry_path):
                continue

            cached_item = cache_map.get(entry)
            if cached_item:
                title_info = self._normalize_title_field(cached_item.get("title"))
                asin = cached_item.get("asin")
            else:
                title_info = {"title": entry.replace("_", " ").title()}
                asin = None

            downloaded.append({
                "slug": entry,
                "title_dir": entry_path,
                "asin": asin,
                "title": title_info
            })

        return downloaded

    def _ensure_local_book_path(self, book):
        slug = book.get("slug")
        if not slug:
            slug = self._slugify_title(self._get_display_title(book))
        title_dir = book.get("title_dir") or os.path.join(artifacts_root_directory, "audiobooks", slug)
        return slug, title_dir

    def _load_clip_metadata_map(self, title_dir_path):
        clips_dir_path = os.path.join(title_dir_path, "clips")
        metadata_path = os.path.join(clips_dir_path, "clip_metadata.json")
        if not os.path.exists(metadata_path):
            return {}
        try:
            with open(metadata_path, "r", encoding="utf-8") as meta_file:
                records = json.load(meta_file)
        except (OSError, json.JSONDecodeError):
            return {}

        metadata_map = {}
        for record in records:
            file_name = record.get("file_name")
            if not file_name:
                continue
            metadata_map[file_name] = record
        return metadata_map

    def _load_library_cache(self):
        if not os.path.exists(self._library_cache_path):
            return
        try:
            with open(self._library_cache_path, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
        except (OSError, json.JSONDecodeError):
            return

        self.library = data.get("library", {})
        self.books = data.get("books", [])

    def _save_library_cache(self):
        payload = {
            "library": self.library,
            "books": self.books
        }
        try:
            with open(self._library_cache_path, "w", encoding="utf-8") as cache_file:
                json.dump(payload, cache_file, indent=2, ensure_ascii=False)
        except OSError as exc:
            print(f"Unable to write library cache: {exc}")
