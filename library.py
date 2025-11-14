import audible

from constants import artifacts_root_directory


class LibraryMixin:
    async def get_book_infos(self, asin):
        async with audible.AsyncClient(self.auth) as client:
            try:
                return await client.get(
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
            except Exception as exc:
                print(exc)
                return None

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
                print(f"No downloaded audiobooks found under {artifacts_root_directory}/audiobooks.")
                return []

        for index, book in enumerate(selection_pool):
            print(f"{index}: {self._get_display_title(book)}")

        selection = input(
            "Enter the index number of the book you would like to use, or enter --all for all available books: \n"
        )

        if selection == "--all":
            return list(selection_pool)

        try:
            chosen = selection_pool[int(selection)]
            return [chosen]
        except (IndexError, ValueError):
            print("Invalid selection")
            return []

    async def cmd_list_books(self):
        await self.get_library(force_refresh=True)
        await self.cmd_show_library()

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
