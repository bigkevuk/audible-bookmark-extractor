"""Integration helpers for Readwise authentication and highlight uploads."""

import os
import json

import requests

from constants import artifacts_root_directory

class Readwise:

  """Persist a Readwise token and expose CLI commands."""
  
  def __init__(self, token):
    """Remember the Readwise API token supplied by the user."""
    self.token = token
  
  @classmethod
  async def authenticate(self) -> "Readwise":
      """Prompt the user for a token, persist it, and return a helper."""
      if os.path.exists(f"{artifacts_root_directory}/secrets/readwise_token.json"):
          print(f"You are already authenticated, to switch accounts, delete secrets directory under {artifacts_root_directory} and try again")          
      token = input("Readwise Token (Go to https://readwise.io/access_token) to get one):")
      
      os.makedirs(f"{artifacts_root_directory}/secrets/", exist_ok=True)
      with open(f"{artifacts_root_directory}/secrets/readwise_token.json", "w") as f:
        f.write(str(token))
      print("Token saved locally successfully")
      
      return Readwise(token)
  
  async def cmd_post_highlights(self, books):
    """Read transcription output for each book and send it to Readwise."""
    if not os.path.exists(f"{artifacts_root_directory}/secrets/readwise_token.json"):
      print("You are not authenticated with readwise. Use the Command readwise-authenticate first")
      return
    
    for book in books:
      print("Posting to Readwise…")
      title = book.get("title", {}).get("title", 'untitled')
      title = title.lower().replace(" ", "_")
      
      with open(f"{artifacts_root_directory}/audiobooks/{title}/trancribed_clips/contents.json", "r") as f:
        highlights = json.load(f)
      
      response = requests.post(url="https://readwise.io/api/v2/highlights/", 
                               headers={"Authorization": f"Token {self.token}"}, 
                               json={"highlights": highlights})

      if response.status_code != 200:
          print(f"Error: {response.status_code}")
          print(response.text)
      else:
          print("Highlights posted successfully")
