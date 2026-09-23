import uvicorn

from .config import Settings


def main():
    settings = Settings()
    uvicorn.run(
        "meeting_protocol.api:create_app",
        factory=True,
        host=settings.public_host,
        port=settings.public_port,
        workers=1,
    )


if __name__ == "__main__":
    main()
