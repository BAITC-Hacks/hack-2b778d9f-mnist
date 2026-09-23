"""Check installed tools, local weights and local inference before a demo."""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
from urllib.request import ProxyHandler, build_opener


def main():
    checks = {}
    for name in ('ffmpeg', 'ffprobe'):
        checks[name] = bool(shutil.which(name))
    versions = {}
    for name, module in [('fastapi', 'fastapi'), ('torch', 'torch'), ('torchaudio', 'torchaudio'),
                         ('transformers', 'transformers'), ('pyannote.audio', 'pyannote')]:
        try:
            versions[name] = importlib.metadata.version(name)
            checks[name] = importlib.util.find_spec(module) is not None
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
            checks[name] = False
    for variable, files in [('ASR_MODEL_PATH', ['config.json', 'tokenizer_config.json']),
                            ('PYANNOTE_DIARIZATION_MODEL', ['config.yaml'])]:
        path = Path(os.environ.get(variable, '/nonexistent'))
        checks[variable] = path.is_dir() and all((path / file).is_file() for file in files)
        checks[variable + '_weights'] = path.is_dir() and any(
            path.rglob('*.safetensors')) if variable == 'ASR_MODEL_PATH' else (
            path.is_dir() and any(path.rglob('*.bin')))
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:11434/api/tags', timeout=5) as response:
            checks['qwen3:8b'] = any(m['name'] == 'qwen3:8b' for m in json.load(response)['models'])
    except Exception:
        checks['qwen3:8b'] = False
    print(json.dumps({'ready': all(checks.values()), 'checks': checks, 'versions': versions,
                      'optional_pdf': bool(shutil.which('libreoffice')),
                      'note': 'Artifact checks only. Run the real smoke to validate loading and quality.'}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
