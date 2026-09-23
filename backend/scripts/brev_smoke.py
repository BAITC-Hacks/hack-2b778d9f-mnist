"""Run an authorized recording through the actual local models and export."""
import argparse
import importlib.metadata
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app


def gpu_used_mib():
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=memory.used', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=5, check=True)
        return int(result.stdout.splitlines()[0].strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def deny_external_python_connections(event, args):
    if event != 'socket.connect' or not isinstance(args[1], tuple):
        return
    try:
        if ipaddress.ip_address(args[1][0]).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError('External network connection blocked by smoke runner')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--meeting-date', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--result', type=Path, help='Private full transcript/result JSON')
    parser.add_argument('--roster', nargs='*', default=[])
    parser.add_argument('--deny-external-python-network', action='store_true')
    args = parser.parse_args()
    if args.deny_external_python_network:
        sys.addaudithook(deny_external_python_connections)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    baseline = gpu_used_mib()
    maximum = [baseline or 0]
    stop = threading.Event()

    def sample_gpu():
        while not stop.wait(1):
            maximum[0] = max(maximum[0], gpu_used_mib() or 0)

    sampler = threading.Thread(target=sample_gpu, daemon=True)
    sampler.start()
    started = time.monotonic()
    report = {'audio': args.audio.name, 'meeting_date': args.meeting_date,
              'network_isolation': os.environ.get('MEETING_NETWORK_ISOLATION', 'none'),
              'network_namespace': os.environ.get('MEETING_NETWORK_NAMESPACE'),
              'network_guard': 'Python process only; separate Ollama process not isolated'
              if args.deny_external_python_network else 'disabled',
              'baseline_gpu_mib': baseline, 'versions': {}}
    for name in ('torch', 'torchaudio', 'transformers', 'pyannote.audio', 'huggingface-hub'):
        try:
            report['versions'][name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            report['versions'][name] = None
    success = False
    try:
        with TestClient(create_app(args.data_dir / 'meetings.sqlite', args.data_dir / 'files')) as client:
            with args.audio.open('rb') as handle:
                created = client.post('/meetings', data={'meeting_date': args.meeting_date,
                                      'roster': json.dumps(args.roster, ensure_ascii=False)},
                                      files={'audio': (args.audio.name, handle, 'application/octet-stream')})
            created.raise_for_status()
            meeting_id = created.json()['id']
            report['meeting_id'] = meeting_id
            client.post(f'/meetings/{meeting_id}/process').raise_for_status()
            deadline = time.monotonic() + 1800
            while True:
                meeting = client.get(f'/meetings/{meeting_id}').json()
                if meeting['status'] in {'review', 'failed'} or time.monotonic() > deadline:
                    break
                time.sleep(1)
            report.update(status=meeting['status'], error=meeting.get('error'),
                          turn_count=len(meeting['transcript']), action_count=len(meeting['actions']),
                          overlap_count=sum(bool(t.get('overlap')) for t in meeting['transcript']),
                          uncertain_speaker_count=sum(bool(t.get('speaker_uncertain')) for t in meeting['transcript']))
            if args.result:
                args.result.parent.mkdir(parents=True, exist_ok=True)
                args.result.write_text(json.dumps(meeting, ensure_ascii=False, indent=2))
            if meeting['status'] == 'review':
                exported = client.post(f'/meetings/{meeting_id}/export')
                exported.raise_for_status()
                report['export'] = exported.json()
                success = client.get(exported.json()['docx']).content.startswith(b'PK')
    except Exception as error:
        report['error'] = str(error)
    finally:
        stop.set()
        sampler.join(timeout=6)
        report.update(elapsed_seconds=round(time.monotonic() - started, 1),
                      peak_gpu_mib=maximum[0], success=success)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
