import os
import json
import sys
import asyncio
import requests
import shutil
import re
import hashlib
import zipfile
from html.parser import HTMLParser
from difflib import SequenceMatcher
from getpass import getpass

try:
    import pandas as pd
except ImportError:
    pd = None
try:
    import whisper
except ImportError:
    whisper = None
try:
    import torch
except ImportError:
    torch = None
try:
    from huggingface_hub import snapshot_download
except ImportError:
    snapshot_download = None
import audible

from pydub import AudioSegment

import speech_recognition as sr
from openai import OpenAI
from PyPDF2 import PdfReader

from errors import ExternalError
from constants import artifacts_root_directory

# not currently in use, but so the user can choose their store
country_code_mapping = {
    "us": ".com",
    "ca": ".ca",
    "uk": ".co.uk",
    "au": ".com.au",
    "fr": ".fr",
    "de": ".de",
    "jp": ".co.jp",
    "it": ".it",
    "in": ".co.in",
    "es": ".es"
}

AUDIBLE_URL_BASE = "https://www.audible"

# set in ms, how long before and after the bookmark timestamp we want to slice the audioclips, useful for redundancy
# i.e to account for the time the user spends to dig up their phone and click bookmark
# Feel free to vary these, but free Speech Recognition API's have certain limits...
START_POSITION_OFFSET = 10000
END_POSITION_OFFSET = 0

class AudibleAPI:

    def __init__(self, auth):
        self.auth = auth
        self.books = []
        self.library = {}
        self._pdf_cache = {}
        self._epub_cache = {}
        self._library_cache_path = os.path.join(os.path.dirname(__file__), "library_cache.json")
        self._load_library_cache()

    @classmethod
    async def authenticate(self) -> "AudibleAPI":
        secrets_dir_path = os.path.join(artifacts_root_directory, "secrets")
        credentials_path = os.path.join(secrets_dir_path, "credentials.json")
        if os.path.exists(credentials_path):
            print(f"You are already authenticated, to switch accounts, delete secrets directory under {artifacts_root_directory} and try again")
        email = input("Audible Email: ")
        password = getpass(
            "Enter Password (will be hidden, press ENTER when done): ")
        print(', '.join(country_code_mapping))
        locale = input("\nPlease enter your locale from the list above: ")

        auth = audible.Authenticator.from_login(
            email,
            password,
            locale=locale,
            with_username=False
        )
        
        os.makedirs(secrets_dir_path, exist_ok=True)
        auth.to_file(credentials_path)
        print("Credentials saved locally successfully")
        return AudibleAPI(auth)

    # Gets information about a book
    async def get_book_infos(self, asin):
        async with audible.AsyncClient(self.auth) as client:
            try:                
                book = await client.get(
                    path=f"library/{asin}",
                    params={
                        "response_groups": (
                            "contributors, media, price, reviews, product_attrs, "
                            "product_extended_attrs, product_desc, product_plan_details, "
                            "product_plans, rating, sample, sku, series, ws4v, origin, "
                            "relationships, review_attrs, categories, badge_types, "
                            "category_ladders, claim_code_url, is_downloaded, pdf_url, "
                            "is_returnable, origin_asin, percent_complete, provided_review"
                        )
                    }
                )
                return book
            except Exception as e:
                print(e)

    # Helper function for displaying the users books and allowing them to select one based on the index number
    async def get_book_selection(self, source="library"):
        if source == "library":
            items = self.library.get("items", [])
            if not items:
                print("No cached Audible library available. Run list_books to refresh it from Audible.")
                return []
            selection_pool = items
        else:
            selection_pool = self._get_downloaded_books()
            if not selection_pool:
                print(f"No downloaded audiobooks found under {os.path.join(artifacts_root_directory, 'audiobooks')}.")
                return []

        for index, book in enumerate(selection_pool):
            print(f"{index}: {self._get_display_title(book)}")

        selection = input(
            "Enter the index number of the book you would like to use, or enter --all for all available books: \n")

        if selection == "--all":
            return list(selection_pool)

        try:
            chosen = selection_pool[int(selection)]
            return [chosen]
        except (IndexError, ValueError):
            print("Invalid selection")
            return []

    # Main download books function
    async def cmd_download_books(self):
        li_books = await self.get_book_selection(source="library")
        if not li_books:
            return

        tasks = []
        for book in li_books:
            tasks.append(
                asyncio.ensure_future(
                    self.get_book_infos(
                        book.get("asin"))))

        books = await asyncio.gather(*tasks)

        all_books = {}

        for book in books:
            if book is not None:
                print(book["item"]["title"])
                asin = book["item"]["asin"]
                raw_title = book["item"]["title"]
                title = raw_title.lower().replace(" ", "_")
                all_books[asin] = title

                # Attempt to download book
                try:
                    re = self.get_download_url(self.generate_url(self.auth.locale.country_code, "download", asin), num_results=1000, response_groups="product_desc, product_attrs")

                # Audible API throws error, usually for free books that are not allowed to be downloaded, we skip to the next
                except audible.exceptions.NetworkError as e:
                    ExternalError(self.get_download_url,
                                  asin, e).show_error()
                    continue

                audible_response = requests.get(re, stream=True)

                title_dir_path = os.path.join(artifacts_root_directory, "audiobooks", title)
                path_exists = os.path.exists(title_dir_path)
                if not path_exists:
                    os.makedirs(title_dir_path)
                    

                if audible_response.ok:
                    title_file_path = os.path.join(title_dir_path, f"{title}.aax")
                    with open(title_file_path, 'wb') as f:
                        print("Downloading %s" % raw_title)

                        total_length = audible_response.headers.get(
                            'content-length')

                        if total_length is None:  # no content length header
                            print(
                                "Unable to estimate download size, downloading, this might take a while...")
                            f.write(audible_response.content)
                        else:
                            # Save book locally and calculate and print download progress (progress bar)
                            dl = 0
                            total_length = int(total_length)
                            for data in audible_response.iter_content(chunk_size=1024*1024):
                                dl += len(data)
                                f.write(data)
                                done = int(50 * dl / total_length)
                                sys.stdout.write("\r[%s%s]" % ('=' * done, ' ' * (50-done)))

                                sys.stdout.write(f"   {int(dl / total_length * 100)}%")
                                sys.stdout.flush()

                else:
                    print(audible_response.text)

    # WIP
    def generate_url(self, country_code, url_type, asin=None):
        if asin and url_type == "download":
            return f"{AUDIBLE_URL_BASE}{country_code_mapping.get(country_code)}/library/download?asin={asin}&codec=AAX"

    # Need the next_request for Audible API to give us the download link for the book
    def get_download_link_callback(self, resp):
        return resp.next_request

    # Sends a request to get the download link for the selected book
    def get_download_url(self, url, **kwargs):

        with audible.Client(auth=self.auth, response_callback=self.get_download_link_callback) as client:
            library = client.get(
                url,
                **kwargs
            )
            return library.url

    async def cmd_list_books(self):
        await self.get_library(force_refresh=True)
        await self.cmd_show_library()
        
    # Gets all books and info for account and adds it to self.books, also returns ASIN for all books
    async def get_library(self, force_refresh=False):
        if self.library and self.books and not force_refresh:
            return [book.get("asin") for book in self.library.get("items", []) if book.get("asin")]

        async with audible.AsyncClient(self.auth) as client:
            self.library = await client.get(
                path="library",
                params={
                    "num_results": 999
                }
            )
            self.books = []
            items = self.library.get("items", [])
            asins = []
            for book in items:
                asin = book.get("asin")
                if asin:
                    asins.append(asin)
                book_title = book.get("title", "Unable to retrieve book name")
                self.books.append(book_title)
            self._save_library_cache()
            return asins

    async def cmd_show_library(self):
        if not self.library.get("items"):
            print("Library cache is empty. Run list_books to refresh from Audible.")
            return
        for index, book in enumerate(self.library.get("items", [])):
            print(f"{index}: {self._get_display_title(book)}")
    
    async def cmd_refresh_library(self):
        print("Refreshing library cache from Audible...")
        await self.get_library(force_refresh=True)
        print(f"Cached {len(self.library.get('items', []))} books locally.")
   

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

        title_aax_path = os.path.join(title_dir_path, f"{title}.aax")
        title_m4b_path = os.path.join(title_dir_path, f"{title}.m4b")
        title_mp3_path = os.path.join(title_dir_path, f"{title}.mp3")

        # Load audiobook into AudioSegment so we can slice it
        audio_book = AudioSegment.from_mp3(title_mp3_path)

        file_counter = 1
        processed_clips = 0
        notes_dict = {}
        clip_metadata_records = []

        # Check whether a folder in clips/ for the book exists or not
        clips_dir_path = os.path.join(title_dir_path, "clips")
        path_exists = os.path.exists(clips_dir_path)
        if not path_exists:
            os.makedirs(clips_dir_path)

        for audio_clip in sorted_records:
            # Get start position to slice
            raw_start_raw = audio_clip.get("startPosition")
            if raw_start_raw is None:
                continue
            raw_start_pos = int(raw_start_raw)

            # If we have a note then we save it so we can use it as the title for the bookmark text
            if audio_clip.get("type", None) in ["audible.note"]:
                notes_dict[raw_start_pos] = audio_clip.get("text")
                print(
                    f"CLIP: {notes_dict[raw_start_pos]}  {raw_start_pos}")

        for clip_entry in combined_clips:
            raw_start_pos = clip_entry.get("startPosition")
            if raw_start_pos is None:
                continue
            start_pos = raw_start_pos - START_POSITION_OFFSET
            end_pos = int(clip_entry.get("endPosition", raw_start_pos + 30000)) + END_POSITION_OFFSET
            if start_pos == end_pos:
                end_pos += 30000

            # Slice it up
            clip = audio_book[start_pos:end_pos]

            note_name = self._resolve_clip_note(notes_dict, clip_entry)
            file_name = note_name if note_name else f"clip{file_counter:04d}"

            # Save the clip
            clip_path = os.path.join(clips_dir_path, f"{file_name}.flac")
            processed_clips += 1
            print(f"[{processed_clips}/{total_clip_entries}] Exporting {file_name}.flac")
            clip.export(clip_path, format="flac")
            file_counter += 1

            clip_metadata_records.append({
                "file_name": file_name,
                "startPosition": raw_start_pos,
                "endPosition": clip_entry.get("endPosition"),
                "note": note_name
            })

        self._save_clip_metadata(clips_dir_path, clip_metadata_records)

    async def cmd_convert_audiobook(self):
        # FFMPEG needs to be installed for this step! see readme for more details
        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return

        for book in li_books:
            # Weird for some reason the title is double nested here, fix later
            _title = self._get_display_title(book)
            if not _title:
                return

            title, title_dir_path = self._ensure_local_book_path(book)
            # Strips Audible DRM  from audiobook
            activation_bytes = self.get_activation_bytes()
            title_aax_path = os.path.join(title_dir_path, f"{title}.aax")
            title_m4b_path = os.path.join(title_dir_path, f"{title}.m4b")
            title_mp3_path = os.path.join(title_dir_path, f"{title}.mp3")
            os.system(
                f"ffmpeg -activation_bytes {activation_bytes} -i {title_aax_path} -c copy {title_m4b_path}")

            # Converts audiobook to .mp3
            os.system(
                f"ffmpeg -i {title_m4b_path} {title_mp3_path}")

    async def cmd_transcribe_bookmarks(self, openai_api_key=None, whisper_mode=None, whisper_model=None, whisper_device=None):
        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return

        global pd
        if pd is None:
            try:
                import pandas as _pd
            except ImportError:
                print("Transcription export requires pandas. Install it with 'pip install pandas'.")
                return
            pd = _pd

        from pandas.io.formats.excel import ExcelFormatter
        ExcelFormatter.header_style = None

        # Determine transcription mode
        transcription_mode = (whisper_mode or ("openai" if openai_api_key else "google")).lower()
        if transcription_mode == "openai" and openai_api_key is None:
            print("OpenAI Whisper mode requested but no API key is configured; falling back to Google Speech Recognition.")
            transcription_mode = "google"
        use_openai = transcription_mode == "openai"
        use_local_whisper = transcription_mode == "local"

        local_whisper_model = None
        local_whisper_transcribe_kwargs = {}
        if use_local_whisper:
            if whisper is None:
                print("Local Whisper requested but the 'whisper' package is not installed. Install it via 'pip install git+https://github.com/openai/whisper.git' (requires PyTorch).")
                return
            if snapshot_download is None:
                print("Local Whisper requested but 'huggingface-hub' is not installed. Install it with 'pip install huggingface-hub'.")
                return
            model_name = whisper_model or "base"
            if whisper_device:
                requested_device = whisper_device
            else:
                requested_device = "cuda" if torch and torch.cuda.is_available() else "cpu"
            if requested_device.startswith("cuda") and (not torch or not torch.cuda.is_available()):
                print("CUDA requested but not available; falling back to CPU for local Whisper.")
                requested_device = "cpu"
            if requested_device == "cpu" or requested_device.startswith("mps"):
                local_whisper_transcribe_kwargs["fp16"] = False
                print(f"Running local Whisper on {requested_device.upper()} with fp16 disabled.")
            model_repo_id = f"openai/whisper-{model_name}"
            whisper_model_root = os.path.join(artifacts_root_directory, "models", "whisper")
            local_model_dir = os.path.join(whisper_model_root, model_name)
            os.makedirs(local_model_dir, exist_ok=True)
            target_model_path = os.path.join(whisper_model_root, f"{model_name}.pt")

            if not os.path.exists(target_model_path):
                print(f"Ensuring Whisper model '{model_name}' is available locally via Hugging Face...")
                try:
                    snapshot_path = snapshot_download(
                        repo_id=model_repo_id,
                        cache_dir=local_model_dir,
                        local_dir=local_model_dir,
                        allow_patterns=["*.bin", "*.json", "*.txt", "*.model", "*.pt", "*.tiktoken"]
                    )
                except Exception as exc:
                    print(f"Failed to download Whisper model '{model_name}' from Hugging Face: {exc}")
                    return

                source_model_path = self._locate_whisper_weights(snapshot_path, model_name)
                if not source_model_path:
                    print(f"Unable to locate model weights inside the downloaded snapshot for '{model_name}'.")
                    return

                try:
                    shutil.copy2(source_model_path, target_model_path)
                except OSError as exc:
                    print(f"Failed to copy Whisper weights to {target_model_path}: {exc}")
                    return

            self._patch_whisper_sha(model_name, target_model_path)

            try:
                print("Loading Whisper model (download cached locally)...")
                local_whisper_model = whisper.load_model(model_name, download_root=whisper_model_root, device=requested_device)
            except Exception as exc:
                print(f"Failed to load Whisper model '{model_name}': {exc}")
                return
            print(f"Using local Whisper model '{model_name}' for transcription")
        elif use_openai:
            client = OpenAI(api_key=openai_api_key)
            print("Using OpenAI Whisper API for transcription")
        else:
            r = sr.Recognizer()
            print("Using Google Speech Recognition for transcription (no API key required)")

        # Create dictionary to store titles and transcriptions and new folder to store transcriptions
        pairs = {}
        jsonHighlights = []
        
        for book in li_books:

            _title = self._get_display_title(book)
            _authors = book.get("title", {}).get("authors", {})
            allAuthors = ", ".join(item['name'] for item in _authors)
            title, title_dir_path = self._ensure_local_book_path(book)
            clips_dir_path = os.path.join(title_dir_path, "clips")
            path_exists = os.path.exists(clips_dir_path)
            if not path_exists:
                os.makedirs(clips_dir_path)

            transcribed_clips_dir_path = os.path.join(title_dir_path, "trancribed_clips")
            trancribed_clips_path_exists = os.path.exists(transcribed_clips_dir_path)
            if not trancribed_clips_path_exists:
                os.makedirs(transcribed_clips_dir_path)

            processed_dir_path = os.path.join(title_dir_path, "proccesedd")
            if not os.path.exists(processed_dir_path):
                os.makedirs(processed_dir_path)

            extracted_text_dir_path = os.path.join(title_dir_path, "extracted text")
            if not os.path.exists(extracted_text_dir_path):
                os.makedirs(extracted_text_dir_path)
            clip_metadata_map = self._load_clip_metadata_map(title_dir_path)

            clip_files = sorted(
                [file for file in os.listdir(clips_dir_path) if file.endswith(".flac")]
            )

            total_clips = len(clip_files)
            if not total_clips:
                print(f"No clips found to transcribe for '{_title}'. Skipping.")
                continue

            print(f"Found {total_clips} clips for '{_title}'. Starting transcription...")

            for idx, file in enumerate(clip_files, start=1):
                highlight = {}
                filename = file
                print(f"[{idx}/{total_clips}] Preparing {filename}")
                highlight["title"] = _title
                highlight["author"] = allAuthors
                if not filename.startswith("clip"):
                    highlight["note"] = filename.replace(".flac", "")
                highlight["source_type"] = "audible_bookmark_extractor"
                if filename.endswith(".flac"):
                    clip_full_path = os.path.join(clips_dir_path, filename)
                    print(f"Processing file: {clip_full_path}")
                    heading = filename.replace(".flac", "")
                    text = ""

                    try:
                        if use_local_whisper and local_whisper_model:
                            print(f"  -> Transcribing {filename} using local Whisper...")
                            result = local_whisper_model.transcribe(
                                clip_full_path,
                                **local_whisper_transcribe_kwargs
                            )
                            text = (result.get("text") or "").strip()
                        elif use_openai:
                            # Use OpenAI Whisper API
                            with open(clip_full_path, "rb") as audio_file:
                                transcription = client.audio.transcriptions.create(
                                    model="gpt-4o-transcribe",
                                    file=audio_file
                                )
                                text = transcription.text
                        else:
                            # Use Google Speech Recognition
                            audioclip = sr.AudioFile(clip_full_path)
                            with audioclip as source:
                                audio = r.record(source)
                            text = r.recognize_google(audio)

                        pairs[str(heading)] = text
                        highlight["text"] = text
                    except Exception as e:
                        highlight["text"] = ""
                        text = ""
                        print(f"Error while recognizing this clip {heading}: {e}")
                    
                    xcel = pd.DataFrame(pairs.values(), index=pairs.keys())

                    # Change header format so that rows can be edited
                    if highlight["text"]:
                        jsonHighlights.append(highlight)
                    
                    # Create writer instance with desired path
                    all_transcriptions_path = os.path.join(transcribed_clips_dir_path, "All_Transcriptions.xlsx")
                    writer = pd.ExcelWriter(
                        all_transcriptions_path, engine='xlsxwriter')

                    # Create a sheet in the same workbook for each file in the directory
                    sheet_name = title[:31].replace(":", "").replace("?", "")
                    xcel.to_excel(writer, sheet_name=sheet_name)
                    workbook = writer.book
                    worksheet = writer.sheets[sheet_name]

                    # Create header format to be used in all headers
                    header_format = workbook.add_format({
                        "valign": "vcenter",
                        "align": "center",
                        "bg_color": "#FFA500",
                        "bold": True,
                        "font_color": "#FFFFFF"})  # transcribe_bookmarks

                    # Set desired cell format
                    cell_format = workbook.add_format()
                    cell_format.set_align("vcenter")
                    cell_format.set_align("center")
                    cell_format.set_text_wrap(True)

                    # Apply header format and format columns to fit data
                    worksheet.write(0, 0, 'Clip Note', header_format)
                    worksheet.write(0, 1, 'Transcription', header_format)
                    worksheet.set_column("B:B", 100)
                    worksheet.set_column("A:A", 50)

                    # Format cells for appropiate size, wrap the text for style points
                    for i in range(1, (len(xcel)+1)):
                        worksheet.set_row(i, 100, cell_format)

                    # Apply changes and save xlsx to Transcribed bookmarks folder.
                    writer.close()

                    processed_file_path = os.path.join(processed_dir_path, filename)
                    if os.path.exists(processed_file_path):
                        os.remove(processed_file_path)
                    shutil.move(clip_full_path, processed_file_path)

                    extracted_text_file_path = os.path.join(extracted_text_dir_path, f"{heading}.json")
                    bookmark_meta = clip_metadata_map.get(heading, {})
                    clip_payload = {
                        "clip": heading,
                        "text": text,
                        "startPosition": bookmark_meta.get("startPosition"),
                        "endPosition": bookmark_meta.get("endPosition")
                    }
                    with open(extracted_text_file_path, "w", encoding="utf-8") as text_file:
                        json.dump(clip_payload, text_file, ensure_ascii=False, indent=2)
            transcription_contents_path = os.path.join(transcribed_clips_dir_path, "contents.json")
            with open(transcription_contents_path, "w") as f:
                json.dump(jsonHighlights, f, indent=4)
    
    async def cmd_search_pdf(self, query=None, threshold=None, max_results=None):
        clip_mode = query is None or not str(query).strip()
        query_value = str(query).strip() if query else ""

        try:
            threshold_value = float(threshold) if threshold is not None else 0.1
        except ValueError:
            print("Invalid threshold supplied; expected a number between 0 and 1.")
            return

        try:
            max_results_value = int(max_results) if max_results is not None else 5
        except ValueError:
            print("Invalid max_results supplied; expected an integer.")
            return

        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return
        for book in li_books:
            raw_title = self._get_display_title(book)
            normalized_title = raw_title.strip() if isinstance(raw_title, str) else "untitled"
            title_slug, title_dir_path = self._ensure_local_book_path(book)
            documents = self._resolve_document_paths(normalized_title, title_dir_path)
            pdf_paths = documents.get("pdf", [])
            epub_paths = documents.get("epub", [])

            if not pdf_paths and not epub_paths:
                print(f"No PDF or EPUB found for '{normalized_title}'. Expected files under {title_dir_path}/pdf or the global pdf directory.")
                continue

            if clip_mode:
                doc_labels = []
                if epub_paths:
                    doc_labels.append(f"{len(epub_paths)} EPUB")
                if pdf_paths:
                    doc_labels.append(f"{len(pdf_paths)} PDF")
                label_text = " and ".join(doc_labels)
                print(f"\nMapping clip transcriptions for '{normalized_title}' using {label_text} document(s)...")
                self._map_clips_to_documents(title_dir_path, pdf_paths, epub_paths, threshold_value)
            else:
                print(f"\nSearching '{normalized_title}' for '{query_value}'...")
                if epub_paths:
                    for epub_doc in epub_paths:
                        matches = self.fuzzy_search_epub(epub_doc, query_value, threshold_value, max_results_value)
                        self._print_document_matches("EPUB", epub_doc, matches, query_value)
                if pdf_paths:
                    for pdf_doc in pdf_paths:
                        matches = self.fuzzy_search_pdf(pdf_doc, query_value, threshold_value, max_results_value)
                        self._print_document_matches("PDF", pdf_doc, matches, query_value)

    def get_activation_bytes(self):

        activation_bytes_path = os.path.join(artifacts_root_directory, "secrets", "activation_bytes.txt")
        # we already have activation bytes
        if os.path.exists(activation_bytes_path):
            with open(activation_bytes_path) as f:
                activation_bytes = f.readlines()[0]

        # we don't, so let's get them
        else:
            activation_bytes = self.auth.get_activation_bytes(
                activation_bytes_path, True)
            text_file = open(activation_bytes_path, "w")
            n = text_file.write(activation_bytes)
            text_file.close()

        return activation_bytes

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

    def fuzzy_search_pdf(self, pdf_path, query, threshold=0.6, max_results=5):
        query_clean = query.strip()
        if not query_clean:
            return []

        matches = []
        pages_text = self._get_cached_pdf_pages(pdf_path)
        if pages_text is None:
            return matches

        for page_number, page_text in enumerate(pages_text, start=1):
            for snippet in self._split_text_snippets(page_text):
                ratio = SequenceMatcher(None, query_clean.lower(), snippet.lower()).ratio()
                if ratio >= threshold:
                    matches.append({
                        "page": page_number,
                        "score": ratio,
                        "snippet": snippet
                    })

        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[:max_results]

    def fuzzy_search_epub(self, epub_path, query, threshold=0.6, max_results=5):
        query_clean = query.strip()
        if not query_clean:
            return []

        sections = self._get_cached_epub_sections(epub_path)
        if sections is None:
            return []

        matches = []
        query_lower = query_clean.lower()
        for section in sections:
            snippet = self._extract_context_snippet(section["text"], query_lower)
            ratio = SequenceMatcher(None, query_lower, snippet.lower()).ratio()
            if query_lower in section["text"].lower():
                ratio = 1.0
            if ratio >= threshold:
                matches.append({
                    "location": section["location"],
                    "score": ratio,
                    "snippet": snippet
                })

        matches.sort(key=lambda item: item["score"], reverse=True)
        return matches[:max_results]

    def _map_clips_to_documents(self, title_dir_path, pdf_paths, epub_paths, threshold):
        extracted_text_dir_path = os.path.join(title_dir_path, "extracted text")
        if not os.path.isdir(extracted_text_dir_path):
            print(f"No extracted text directory found for this book at {extracted_text_dir_path}.")
            return

        clip_texts = self._collect_clip_texts(extracted_text_dir_path)
        if not clip_texts:
            print(f"No clip transcription files were found in {extracted_text_dir_path}.")
            return

        references = []
        total_clips = len(clip_texts)
        processed_text_dir_path = os.path.join(extracted_text_dir_path, "processed")
        os.makedirs(processed_text_dir_path, exist_ok=True)
        output_path = os.path.join(title_dir_path, "clip_pdf_references.json")

        for index, (clip_name, clip_text, clip_path, start_position, end_position) in enumerate(clip_texts, start=1):
            print(f"Processing clip {index}/{total_clips}: {clip_name}")
            cleaned_text = clip_text.strip()
            entry = {
                "clip": clip_name,
                "clip_text": cleaned_text,
                "bookmark_start_ms": start_position,
                "bookmark_end_ms": end_position,
                "references": {}
            }

            if not cleaned_text:
                reason = "Transcription is empty."
                if epub_paths:
                    entry["references"]["epub"] = {"matched": False, "reason": reason}
                else:
                    entry["references"]["epub"] = {"matched": False, "reason": "No EPUB documents available."}
                if pdf_paths:
                    entry["references"]["pdf"] = {"matched": False, "reason": reason}
                else:
                    entry["references"]["pdf"] = {"matched": False, "reason": "No PDF documents available."}
                references.append(entry)
                self._write_clip_reference_summary(output_path, references)
                self._move_processed_text(clip_path, processed_text_dir_path)
                continue

            pdf_query_text = cleaned_text
            pdf_threshold = threshold

            if epub_paths:
                epub_match = self._match_clip_against_epub(epub_paths, cleaned_text, threshold)
                entry["references"]["epub"] = epub_match
                if epub_match.get("matched"):
                    pdf_query_text = epub_match.get("snippet") or cleaned_text
                    pdf_threshold = max(threshold, 0.5)
            else:
                entry["references"]["epub"] = {"matched": False, "reason": "No EPUB documents available."}

            if pdf_paths:
                pdf_match = self._match_clip_against_pdfs(pdf_paths, pdf_query_text, pdf_threshold)
                entry["references"]["pdf"] = pdf_match
            else:
                entry["references"]["pdf"] = {"matched": False, "reason": "No PDF documents available."}

            references.append(entry)

            self._write_clip_reference_summary(output_path, references)
            self._move_processed_text(clip_path, processed_text_dir_path)

        print(f"Wrote clip reference summary to {output_path}")

    def _write_clip_reference_summary(self, output_path, references):
        try:
            with open(output_path, "w", encoding="utf-8") as output_file:
                json.dump(references, output_file, indent=2, ensure_ascii=False)
        except OSError as exc:
            print(f"Failed to write clip reference summary: {exc}")

    def _print_document_matches(self, doc_type, doc_path, matches, query_value):
        doc_label = f"{doc_type} ({doc_path})"
        if not matches:
            print(f"{doc_label}: no matches for '{query_value}'.")
            return
        print(f"{doc_label}:")
        for idx, match in enumerate(matches, start=1):
            location = match.get("page")
            if location is None:
                location = match.get("location", "N/A")
            score = match.get("score", 0)
            snippet = match.get("snippet", "")
            print(f"  {idx}. [location {location}, score {score:.2f}] {snippet}")

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

    def _collect_clip_texts(self, extracted_text_dir_path):
        clip_texts = []
        try:
            files = sorted(os.listdir(extracted_text_dir_path))
        except OSError as exc:
            print(f"Unable to read extracted text directory {extracted_text_dir_path}: {exc}")
            return clip_texts

        metadata_map = self._load_clip_metadata_map(os.path.dirname(extracted_text_dir_path))

        for file_name in files:
            file_path = os.path.join(extracted_text_dir_path, file_name)
            base, ext = os.path.splitext(file_name)
            ext_lower = ext.lower()
            if ext_lower == ".json":
                try:
                    with open(file_path, "r", encoding="utf-8") as clip_file:
                        data = json.load(clip_file)
                    text = data.get("text", "")
                    start_position = data.get("startPosition")
                    end_position = data.get("endPosition")
                except (OSError, json.JSONDecodeError) as exc:
                    print(f"Failed to read {file_path}: {exc}")
                    continue
            elif ext_lower == ".txt":
                try:
                    with open(file_path, "r", encoding="utf-8") as clip_file:
                        text = clip_file.read()
                except OSError as exc:
                    print(f"Failed to read {file_path}: {exc}")
                    continue
                meta = metadata_map.get(base, {})
                start_position = meta.get("startPosition")
                end_position = meta.get("endPosition")
            else:
                continue

            clip_texts.append((base, text, file_path, start_position, end_position))

        return clip_texts

    def _move_processed_text(self, source_path, processed_dir_path):
        if not os.path.exists(source_path):
            return
        dest_path = os.path.join(processed_dir_path, os.path.basename(source_path))
        try:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            shutil.move(source_path, dest_path)
        except OSError as exc:
            print(f"Failed to move processed text file {source_path}: {exc}")

    def _match_clip_against_epub(self, epub_paths, query_text, threshold):
        query_clean = query_text.strip()
        if not query_clean:
            return {"matched": False, "reason": "Transcription is empty."}

        best_match = None
        for epub_path in epub_paths:
            sections = self._get_cached_epub_sections(epub_path)
            if not sections:
                continue
            match = self._best_epub_match(sections, query_clean, threshold)
            if match:
                match["path"] = epub_path
                if best_match is None or match["score"] > best_match["score"]:
                    best_match = match

        if best_match:
            return {
                "matched": True,
                "path": best_match["path"],
                "location": best_match["location"],
                "score": round(best_match["score"], 3),
                "snippet": best_match["snippet"]
            }

        return {"matched": False, "reason": f"No EPUB location exceeded similarity threshold {threshold}."}

    def _match_clip_against_pdfs(self, pdf_paths, query_text, threshold):
        query_clean = query_text.strip()
        if not query_clean:
            return {"matched": False, "reason": "Transcription is empty."}

        best_match = None
        for pdf_path in pdf_paths:
            pages_text = self._get_cached_pdf_pages(pdf_path)
            if pages_text is None:
                continue
            match = self._best_pdf_match(pages_text, query_clean, threshold)
            if match:
                match["path"] = pdf_path
                if best_match is None or match["score"] > best_match["score"]:
                    best_match = match

        if best_match:
            return {
                "matched": True,
                "path": best_match["path"],
                "page": best_match["page"],
                "score": round(best_match["score"], 3),
                "snippet": best_match["snippet"]
            }

        return {"matched": False, "reason": f"No PDF location exceeded similarity threshold {threshold}."}

    def _best_pdf_match(self, pages_text, query, threshold):
        query_clean = query.strip()
        if not query_clean:
            return None

        best_match = None
        lc_query = query_clean.lower()

        for page_number, page_text in enumerate(pages_text, start=1):
            for snippet in self._split_text_snippets(page_text):
                ratio = SequenceMatcher(None, lc_query, snippet.lower()).ratio()
                if ratio >= threshold and (best_match is None or ratio > best_match["score"]):
                    best_match = {
                        "page": page_number,
                        "score": ratio,
                        "snippet": snippet
                    }

        return best_match

    def _best_epub_match(self, sections, query, threshold):
        query_clean = query.strip()
        if not query_clean:
            return None

        best_match = None
        query_lower = query_clean.lower()

        for section in sections:
            text = section["text"]
            lower_text = text.lower()
            snippet = self._extract_context_snippet(text, query_lower)
            ratio = SequenceMatcher(None, query_lower, snippet.lower()).ratio()
            if query_lower in lower_text:
                ratio = 1.0
            if ratio >= threshold and (best_match is None or ratio > best_match["score"]):
                best_match = {
                    "location": section["location"],
                    "snippet": snippet,
                    "score": ratio
                }

        return best_match

    def _extract_context_snippet(self, text, query_lower, window=150):
        lower_text = text.lower()
        idx = lower_text.find(query_lower)
        if idx == -1:
            return text[:window].strip()
        start = max(idx - window // 2, 0)
        end = min(idx + len(query_lower) + window // 2, len(text))
        return text[start:end].strip()

    def _split_text_snippets(self, text):
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []

        snippets = re.split(r'(?<=[.!?])\s+', normalized)
        snippets = [snippet.strip() for snippet in snippets if snippet.strip()]
        if not snippets:
            return [normalized]
        return snippets

    def _resolve_document_paths(self, book_title, title_dir_path=None):
        normalized_key = self._normalized_key(book_title)

        search_directories = []
        if title_dir_path:
            search_directories.extend([
                os.path.join(title_dir_path, "pdf"),
                os.path.join(title_dir_path, "epub"),
                title_dir_path
            ])

        pdf_root = os.path.join(artifacts_root_directory, "pdf")
        if pdf_root not in search_directories:
            search_directories.append(pdf_root)

        epub_root = os.path.join(artifacts_root_directory, "epub")
        if epub_root not in search_directories:
            search_directories.append(epub_root)

        doc_map = {"pdf": [], "epub": []}
        seen_paths = set()

        for folder in search_directories:
            if not os.path.isdir(folder):
                continue
            for file_name in os.listdir(folder):
                base, ext = os.path.splitext(file_name)
                ext_lower = ext.lower()
                if ext_lower not in [".pdf", ".epub"]:
                    continue
                key = self._normalized_key(base)
                if key != normalized_key:
                    continue
                full_path = os.path.join(folder, file_name)
                abs_path = os.path.abspath(full_path)
                if abs_path in seen_paths:
                    continue
                seen_paths.add(abs_path)
                if ext_lower == ".pdf":
                    doc_map["pdf"].append(full_path)
                else:
                    doc_map["epub"].append(full_path)

        return doc_map

    def _resolve_pdf_path(self, book_title, title_dir_path=None):
        documents = self._resolve_document_paths(book_title, title_dir_path)
        pdf_paths = documents.get("pdf", [])
        return pdf_paths[0] if pdf_paths else None

    def _normalized_key(self, value):
        if not isinstance(value, str):
            value = str(value)
        return re.sub(r"[^a-z0-9]", "", value.lower())

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

    def _get_cached_pdf_pages(self, pdf_path):
        abs_path = os.path.abspath(pdf_path)
        try:
            last_modified = os.path.getmtime(abs_path)
        except OSError as exc:
            print(f"Unable to access PDF at {pdf_path}: {exc}")
            return None

        cache_entry = self._pdf_cache.get(abs_path)
        if cache_entry and cache_entry.get("last_modified") == last_modified:
            return cache_entry.get("pages")

        pages = self._extract_pdf_pages(abs_path)
        if pages is None:
            return None

        self._pdf_cache[abs_path] = {
            "pages": pages,
            "last_modified": last_modified
        }
        return pages

    def _get_cached_epub_sections(self, epub_path):
        abs_path = os.path.abspath(epub_path)
        try:
            last_modified = os.path.getmtime(abs_path)
        except OSError as exc:
            print(f"Unable to access EPUB at {epub_path}: {exc}")
            return None

        cache_entry = self._epub_cache.get(abs_path)
        if cache_entry and cache_entry.get("last_modified") == last_modified:
            return cache_entry.get("sections")

        sections = self._extract_epub_sections(abs_path)
        self._epub_cache[abs_path] = {
            "sections": sections,
            "last_modified": last_modified
        }
        return sections

    def _extract_epub_sections(self, epub_path):
        sections = []
        try:
            with zipfile.ZipFile(epub_path, "r") as zf:
                for file_name in zf.namelist():
                    if not file_name.lower().endswith((".xhtml", ".html", ".htm")):
                        continue
                    try:
                        data = zf.read(file_name).decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    text = self._strip_html(data)
                    clean_text = re.sub(r"\s+", " ", text).strip()
                    if clean_text:
                        sections.append({
                            "text": clean_text,
                            "location": file_name,
                            "source": epub_path
                        })
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"Unable to read EPUB at {epub_path}: {exc}")
        return sections

    def _strip_html(self, html_text):
        stripper = _HTMLStripper()
        try:
            stripper.feed(html_text)
        except Exception:
            pass
        return stripper.get_data()

    def _extract_pdf_pages(self, pdf_path):
        pages = []
        try:
            reader = PdfReader(pdf_path)
        except Exception as exc:
            print(f"Unable to read PDF at {pdf_path}: {exc}")
            return None

        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                print(f"Failed to extract text from page {page_number}: {exc}")
                page_text = ""
            pages.append(page_text)

        return pages

    def _locate_whisper_weights(self, snapshot_root, model_name):
        preferred_names = [
            f"{model_name}.pt",
            f"{model_name}.bin",
            "model.bin",
            "pytorch_model.bin",
        ]

        for root, _dirs, files in os.walk(snapshot_root):
            for name in preferred_names:
                if name in files:
                    return os.path.join(root, name)

        fallback_extensions = (".bin", ".pt", ".model")
        for root, _dirs, files in os.walk(snapshot_root):
            for file_name in files:
                if file_name.endswith(fallback_extensions):
                    return os.path.join(root, file_name)

        return None

    def _patch_whisper_sha(self, model_name, model_path):
        if not whisper or not os.path.exists(model_path):
            return
        try:
            from whisper import _download  # type: ignore
            models = getattr(_download, "_MODELS", None)
            if not isinstance(models, dict):
                return
            models.setdefault(model_name, {})
            models[model_name]["sha256"] = self._compute_file_sha(model_path)
        except Exception:
            return

    def _compute_file_sha(self, file_path):
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _extract_pdf_pages(self, pdf_path):
        pages = []
        try:
            reader = PdfReader(pdf_path)
        except Exception as exc:
            print(f"Unable to read PDF at {pdf_path}: {exc}")
            return None

        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:
                print(f"Failed to extract text from page {page_number}: {exc}")
                page_text = ""
            pages.append(page_text)

        return pages


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self._chunks = []

    def handle_data(self, data):
        self._chunks.append(data)

    def get_data(self):
        return " ".join(self._chunks)
