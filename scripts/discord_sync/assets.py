"""Resolve each portrait to the last commit that changed that image."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def resolve_assets(repo_root: Path, config: dict) -> dict:
    result = dict(config)
    refs = dict(config.get('asset_refs', {}))
    for image in sorted((repo_root / 'wiki/images').glob('*')):
        if not image.is_file() or image.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            continue
        relative = image.relative_to(repo_root).as_posix()
        response = subprocess.run(
            ['git', '-C', str(repo_root), 'log', '-1', '--format=%H', '--', relative],
            capture_output=True, text=True, check=True, timeout=10,
        )
        revision = response.stdout.strip()
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('A tracked portrait has no immutable Git revision.')
        refs[relative] = revision
    result['asset_refs'] = refs
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--config', type=Path, default=Path('.discord/sync.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    config_path = args.config if args.config.is_absolute() else root / args.config
    config = json.loads(config_path.read_text())
    resolved = resolve_assets(root, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(resolved, indent=2) + '\n')
    print(json.dumps({'portrait_revisions': len(resolved['asset_refs'])}))


if __name__ == '__main__':
    main()
