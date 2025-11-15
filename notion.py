"""Minimal Notion API exporter stub."""

import requests
import os
from datetime import datetime

class NotionExporter:
    """Send extracted highlights to a target Notion database."""

    @staticmethod
    def post_notion(text_heading, text_content):
        """Publish a highlight to Notion using environment-based credentials."""
        token = os.environ.get("NOTION_TOKEN")
        url = "https://api.notion.com/v1/pages"

        data = {
            "parent": {"database_id": "1b9cbc3855e942e6b6ecf9bc0bde3a49"},
            "properties": {
                "Heading": {
                    "title": [
                        {
                            "text": {
                                "content": text_heading
                            }
                        }
                    ]
                },
                "Content": {
                    "rich_text": [
                        {
                            "text": {
                                "content": text_content
                            }
                        }
                    ]
                }
            }}
        import json
        data = json.dumps(data)

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Notion-Version": "2022-02-22",
            "Content-Type": "application/json"
        }

        response = requests.post(url, headers=headers, data=data)

        print(response.text)
