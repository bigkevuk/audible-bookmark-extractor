import os
from getpass import getpass

import audible

from constants import artifacts_root_directory

from base import AudibleBase
from bookmarks import BookmarkMixin
from config import country_code_mapping
from documents import DocumentSearchMixin
from downloads import DownloadMixin
from library import LibraryMixin
from transcription import TranscriptionMixin


class AudibleAPI(
    DocumentSearchMixin,
    TranscriptionMixin,
    BookmarkMixin,
    DownloadMixin,
    LibraryMixin,
    AudibleBase,
):
    def __init__(self, auth):
        super().__init__(auth)

    @classmethod
    async def authenticate(cls) -> "AudibleAPI":
        secrets_dir_path = os.path.join(artifacts_root_directory, "secrets")
        credentials_path = os.path.join(secrets_dir_path, "credentials.json")
        if os.path.exists(credentials_path):
            print(f"You are already authenticated, to switch accounts, delete secrets directory under {artifacts_root_directory} and try again")
        email = input("Audible Email: ")
        password = getpass("Enter Password (will be hidden, press ENTER when done): ")
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
        return cls(auth)
