import asyncio
import sys

try:
    from command import Command
except ModuleNotFoundError as exc:
    if exc.name == "audible":
        print("The 'audible' package is not installed. Please run 'pip install -r requirements.txt' before starting the app.")
        sys.exit(1)
    raise

async def main():
    cmd = Command()
    cmd.welcome()
    try:
        await cmd.command_loop()
    except KeyboardInterrupt:
        print("\nExiting...")

if __name__ == "__main__":
    asyncio.run(main())
