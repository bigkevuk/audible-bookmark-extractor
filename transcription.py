import json
import os
import shutil

import speech_recognition as sr
from openai import OpenAI

from constants import artifacts_root_directory

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

import hashlib


class TranscriptionMixin:
    async def cmd_transcribe_bookmarks(self, openai_api_key=None, whisper_mode=None, whisper_model=None, whisper_device=None):
        li_books = await self.get_book_selection(source="local")
        if not li_books:
            return

        global pd
        if pd is None:
            try:
                import pandas as _pd  # type: ignore
            except ImportError:
                print("Transcription export requires pandas. Install it with 'pip install pandas'.")
                return
            pd = _pd

        from pandas.io.formats.excel import ExcelFormatter  # type: ignore
        ExcelFormatter.header_style = None

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

        pairs = {}
        jsonHighlights = []

        for book in li_books:
            _title = self._get_display_title(book)
            _authors = book.get("title", {}).get("authors", {})
            allAuthors = ", ".join(item['name'] for item in _authors)
            title, title_dir_path = self._ensure_local_book_path(book)
            clips_dir_path = os.path.join(title_dir_path, "clips")
            os.makedirs(clips_dir_path, exist_ok=True)

            transcribed_clips_dir_path = os.path.join(title_dir_path, "trancribed_clips")
            os.makedirs(transcribed_clips_dir_path, exist_ok=True)

            processed_dir_path = os.path.join(title_dir_path, "proccesedd")
            os.makedirs(processed_dir_path, exist_ok=True)

            extracted_text_dir_path = os.path.join(title_dir_path, "extracted text")
            os.makedirs(extracted_text_dir_path, exist_ok=True)
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
                            with open(clip_full_path, "rb") as audio_file:
                                transcription = client.audio.transcriptions.create(
                                    model="gpt-4o-transcribe",
                                    file=audio_file
                                )
                                text = transcription.text
                        else:
                            audioclip = sr.AudioFile(clip_full_path)
                            with audioclip as source:
                                audio = r.record(source)
                            text = r.recognize_google(audio)

                        pairs[str(heading)] = text
                        highlight["text"] = text
                    except Exception as exc:
                        highlight["text"] = ""
                        text = ""
                        print(f"Error while recognizing this clip {heading}: {exc}")

                    xcel = pd.DataFrame(pairs.values(), index=pairs.keys())

                    if highlight["text"]:
                        jsonHighlights.append(highlight)

                    all_transcriptions_path = os.path.join(transcribed_clips_dir_path, "All_Transcriptions.xlsx")
                    writer = pd.ExcelWriter(all_transcriptions_path, engine='xlsxwriter')

                    sheet_name = title[:31].replace(":", "").replace("?", "")
                    xcel.to_excel(writer, sheet_name=sheet_name)
                    workbook = writer.book
                    worksheet = writer.sheets[sheet_name]

                    header_format = workbook.add_format({
                        "valign": "vcenter",
                        "align": "center",
                        "bg_color": "#FFA500",
                        "bold": True,
                        "font_color": "#FFFFFF"})

                    cell_format = workbook.add_format()
                    cell_format.set_align("vcenter")
                    cell_format.set_align("center")
                    cell_format.set_text_wrap(True)

                    worksheet.write(0, 0, 'Clip Note', header_format)
                    worksheet.write(0, 1, 'Transcription', header_format)
                    worksheet.set_column("B:B", 100)
                    worksheet.set_column("A:A", 50)

                    for i in range(1, (len(xcel) + 1)):
                        worksheet.set_row(i, 100, cell_format)

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
                    chapter_title = bookmark_meta.get("chapterTitle")
                    if chapter_title:
                        clip_payload["chapterTitle"] = chapter_title
                    chapter_index = bookmark_meta.get("chapterIndex")
                    if chapter_index is not None:
                        clip_payload["chapterIndex"] = chapter_index
                    with open(extracted_text_file_path, "w", encoding="utf-8") as text_file:
                        json.dump(clip_payload, text_file, ensure_ascii=False, indent=2)
            transcription_contents_path = os.path.join(transcribed_clips_dir_path, "contents.json")
            with open(transcription_contents_path, "w") as f:
                json.dump(jsonHighlights, f, indent=4)

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
