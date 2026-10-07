"""Publish generated commits without overwriting concurrent library/code updates."""
import argparse
import subprocess
import sys
import time
from pathlib import Path


def git(repo, *args, check=True):
    return subprocess.run(['git', *args], cwd=repo, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def push_updates(repo, refresh_read_filter=False, attempts=3):
    git(repo, 'config', 'user.name', 'github-actions[bot]')
    git(repo, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    for attempt in range(attempts):
        git(repo, 'fetch', 'origin', 'main')
        result = git(repo, 'rebase', 'origin/main', check=False)
        if result.returncode:
            git(repo, 'rebase', '--abort', check=False)
            raise RuntimeError('Concurrent changes conflict with generated feeds. '
                               'Preserved remote commits; recover output from the workflow artifact.\n'
                               + result.stderr)
        # A read-history upload may have arrived while feeds were generating.
        if refresh_read_filter:
            subprocess.run([
                sys.executable, 'rss_read_filter.py', '--root', '.',
                '--suppression', 'read-suppression.json', '--glob', '*.xml',
                '--glob', 'conference-feeds/*.xml', '--glob', 'official-feeds/*.xml',
                '--glob', 'journal-feeds/*.xml',
            ], cwd=repo, check=True)
            git(repo, 'add', '-u', '--', '*.xml', 'official-feeds/*.xml', 'journal-feeds/*.xml')
            if git(repo, 'diff', '--cached', '--quiet', check=False).returncode:
                git(repo, 'commit', '-m', 'Apply latest RSS read suppression')
        result = git(repo, 'push', 'origin', 'HEAD:main', check=False)
        if result.returncode == 0:
            return
        if attempt + 1 == attempts:
            raise RuntimeError('Push failed after bounded retries; no force push used.\n' + result.stderr)
        time.sleep(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh-read-filter', action='store_true')
    args = parser.parse_args()
    push_updates(Path.cwd(), args.refresh_read_filter)
