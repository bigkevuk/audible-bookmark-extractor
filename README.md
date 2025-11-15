# Audible Bookmark Extractor

An interactive command-line tool that helps you download your Audible audiobooks, extract any bookmarks you saved while listening, and turn those bookmarks into searchable text or exported highlights.

## What This Tool Does
- Logs in to Audible on your behalf, downloads the `.aax` files you already own, and stores them in `~/audible-bookmark-extractor`.
- Converts each audiobook into formats that are easy to slice (`.m4b` and `.mp3`).
- Pulls every bookmark/note for a book, creates short `.flac` clips around each timestamp, and transcribes the clips.
- Exports transcripts to Excel/JSON and can send the highlights to Readwise (which can sync with Notion, Obsidian, etc.).

## How the Pieces Fit Together
- `main.py` and `command.py` start an async REPL-style CLI that routes commands such as `download_books` or `transcribe_bookmarks`.
- `audible_api.py` talks to Audible: listing titles, downloading files, slicing audio, and transcribing clips (either via OpenAI Whisper or Google Speech Recognition).
- `readwise.py` uploads the finished highlights to Readwise.
- `openai_config.py` and `auth.py` store the tokens/credentials needed for the integrations.
- `constants.py` centralizes where artifacts are written, and `errors.py` prints easy-to-read status messages.

Each function in the codebase now has docstrings so you can open any file and quickly understand what it is responsible for.

## Quick Start for Beginners
1. **Install Python dependencies**
   ```bash
   pip install -r requirements.txt
   ```
2. **Install FFMPEG** (required for converting `.aax` files). Follow the instructions for your OS in the [ffmpeg-python docs](https://github.com/kkroening/ffmpeg-python).
3. **Run the CLI**
   ```bash
   python main.py
   ```
4. **Type `help`** to see the available commands and follow the prompts.

The app writes everything under `~/audible-bookmark-extractor`. Inside you will find per-book folders with raw audio, sliced clips, Excel files, and JSON highlight payloads.

## Command Reference

| Command | Purpose |
| --- | --- |
| `help` | Print all commands and their descriptions. |
| `authenticate` | Log in to Audible and save an encrypted credential file. Required for most commands. |
| `list_books` / `show_library` | Display the titles that Audible reports for your account. |
| `download_books` | Download one or all books you select. Shows a progress bar while streaming the `.aax` file. |
| `convert_audiobook` | Remove DRM (requires activation bytes + FFMPEG) and create `.m4b`/`.mp3` versions. |
| `get_bookmarks` | Fetch bookmark metadata and slice `.flac` clips for each timestamp. |
| `transcribe_bookmarks` | Turn the `.flac` clips into text. Uses OpenAI Whisper if `openai_authenticate` has been run; otherwise falls back to Google Speech Recognition (no key needed). |
| `readwise_authenticate` | Store your Readwise token. |
| `readwise_post_highlights` | Upload the generated JSON highlights to Readwise. |
| `openai_authenticate` | Persist an OpenAI API key so the app can call Whisper. |
| `quit` / `exit` | Leave the CLI. |

Arguments can be passed as `command --key=value`. See the docstrings inside `command.py` for the currently supported flags.

## Authentication

### Audible (required)
1. Choose `authenticate` in the CLI.
2. Provide your Audible email, password, and locale (e.g., `us`, `uk`, `de`).
3. Complete the CAPTCHA in the browser window if prompted. Two-factor authentication must be enabled on your Amazon account.
4. Credentials are stored in `~/audible-bookmark-extractor/secrets/credentials.json`. Delete this file if you need to re-authenticate.

### Readwise (optional, for exporting)
1. Go to [readwise.io/access_token](https://readwise.io/access_token) and copy your token.
2. Run `readwise_authenticate` and paste the token when prompted.
3. Highlights live in `~/audible-bookmark-extractor/<book>/trancribed_clips/contents.json`. After running `readwise_post_highlights` you can visit [readwise.io/books](https://readwise.io/books) to confirm the upload.

### OpenAI Whisper (optional, for premium transcription)
1. Visit [platform.openai.com/api-keys](https://platform.openai.com/api-keys) to generate a key.
2. Run `openai_authenticate` and paste the key. The CLI stores it in `~/audible-bookmark-extractor/secrets/openai_key.json`.
3. Whisper provides higher accuracy than the Google fallback, especially for noisy audio.

If you skip the OpenAI step, the transcription command will use the SpeechRecognition library’s Google recognizer automatically.

## Typical Workflow
1. `authenticate`
2. `download_books`
3. `convert_audiobook`
4. `get_bookmarks`
5. `transcribe_bookmarks`
6. (Optional) `readwise_authenticate` and `readwise_post_highlights`

Each step presents a numbered list of books so you can focus on one title or process the entire library with `--all`.

## File Layout Cheat Sheet
- `~/audible-bookmark-extractor/secrets/` – Credentials (Audible, Readwise, OpenAI) plus the activation bytes used for DRM removal.
- `~/audible-bookmark-extractor/audiobooks/<title>/` – Book-specific folder containing the `.aax`, `.m4b`, `.mp3`, sliced clips, Excel export, and `contents.json`.
- `audible_api.py` – Core download, conversion, slicing, and transcription logic.
- `readwise.py` – Uploads JSON highlights to Readwise.
- `openai_config.py` / `auth.py` – Small helpers that prompt the user and store tokens.
- `notion.py` – Prototype for sending the same highlights to a Notion database.

Open each file to see inline docstrings that explain inputs, side effects, and important edge cases.

## Transcription Notes
- Clips are saved as `.flac` for better results with both Whisper and Google’s recognizer.
- Excel exports use `xlsxwriter` so you can keep editing your notes manually—column widths and formats are applied automatically.
- The JSON file is the exact payload sent to the Readwise API. You can reuse it for other integrations (Notion, Obsidian, etc.).

## FFMPEG Setup Reminder
FFMPEG must be installed on your system in addition to `ffmpeg-python`. Check your OS package manager or the [ffmpeg project](https://ffmpeg.org/download.html) for binaries. Without FFMPEG, `convert_audiobook` cannot run and the downstream slicing/transcription steps will fail.

## Anti-Piracy Notice
This project does not crack Audible DRM or assist in downloading audiobooks you do not own. It allows you to use your own encryption key (retrieved from Audible’s servers) to decrypt audiobooks—mirroring the process used by Audible’s official software. This only applies to audiobooks legally purchased by you.

The tool is intended solely for personal use, such as archiving, converting, or managing your legally purchased content. Decrypted audiobooks must not be distributed through public servers, torrents, or any other mass distribution channels. Support will not be provided to those involved in such activities. Authors, retailers, and publishers depend on fair compensation to continue producing high-quality audiobooks—please respect their rights.

This software is best used as a backup solution for your own audiobooks, or to export your bookmarks to text/Excel for personal study.

## FAQ

**Error:** `sh: 1: ffmpeg: not found`  
**Solution:** Install FFMPEG. For details, refer to the [python-ffmpeg documentation](https://github.com/kkroening/ffmpeg-python).

---

**Issue:** I’m prompted for a CVF code during authentication, and the program crashes.  
**Solution:** Ensure that Two-Factor Authentication (2FA) is enabled on your Amazon account.

---

**Error:** `Exception: Login failed. Please check the log.`  
**Solution:** 2FA must be enabled on your Amazon/Audible account. Retry after confirming.
