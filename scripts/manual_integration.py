"""Optional integration runner. Requires the actual running local app and models."""

import argparse
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--date")
    args = parser.parse_args()
    load_dotenv()
    candidates = sorted(
        p for p in args.audio_dir.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".mp4"}
    )
    if len(candidates) != 1:
        raise SystemExit("Expected exactly one audio file in --audio-dir")
    headers = {"X-Agent-API-Key": os.getenv("AGENT_API_KEY", "")}
    with httpx.Client(
        base_url="http://127.0.0.1:" + os.getenv("PUBLIC_PORT", "27361"),
        headers=headers,
        timeout=120,
        trust_env=False,
    ) as client:
        response = client.post(
            "/api/v1/meetings", json={"title": args.title, "meeting_date": args.date}
        )
        response.raise_for_status()
        mid = response.json()["id"]
        prefix = "/api/v1/meetings/" + mid
        with candidates[0].open("rb") as stream:
            response = client.post(prefix + "/audio", files={"file": (candidates[0].name, stream)})
            response.raise_for_status()
        client.post(prefix + "/process").raise_for_status()
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            response = client.get(prefix)
            response.raise_for_status()
            meeting = response.json()
            print(meeting["status"], flush=True)
            if meeting["status"] == "failed":
                raise SystemExit(meeting["error"])
            if meeting["status"] == "completed":
                break
            time.sleep(3)
        else:
            raise SystemExit("Timed out waiting for processing; inspect the app dashboard.")
        output = Path("data/manual") / mid
        output.mkdir(parents=True, exist_ok=True)
        (output / "meeting.json").write_text(
            json.dumps(meeting, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for kind in ["docx", "pdf"]:
            response = client.get(prefix + "/export." + kind)
            response.raise_for_status()
            (output / ("protocol." + kind)).write_bytes(response.content)
        print("Saved results to", output)


if __name__ == "__main__":
    main()
