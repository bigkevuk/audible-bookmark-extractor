import json
import os
import re
import shutil
import zipfile
from difflib import SequenceMatcher
from html.parser import HTMLParser

from PyPDF2 import PdfReader

from constants import artifacts_root_directory


class DocumentSearchMixin:
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
