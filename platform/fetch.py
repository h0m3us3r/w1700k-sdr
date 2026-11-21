#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Download and verify the pinned upstream sources listed in platform/sources.json.

    platform/fetch.py [buildroot|openwrt|linux|u-boot ...]   # default: all

Archives are checked against their SHA-256 and git checkouts against their
commit. An archive may name mirrors, tried in order after its first address. Everything is cached under platform/build/downloads and platform/build/src,
so a second run does nothing.

W1700K_OPENWRT_GIT overrides the OpenWrt repository URL (for example a local
mirror, as file:///path/to/openwrt); the pinned commit is still enforced.
"""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from buildlib import (DOWNLOADS, SRC, BuildError, main_guard, output, require, run,
                      sha256_file, sources)

DOWNLOAD_TIMEOUT = 30       # seconds for a site to answer, and between two pieces of its answer


def download(url, sha256, name=None, mirrors=()):
    """Fetch `url` into the download cache once and return the verified file.

    `mirrors`: other places that keep the same file, tried in order when the one before does not
    give it. Whichever answers, the file is held to its SHA-256, so a mirror is trusted with
    nothing; and so is a file put into the cache by hand.
    """
    target = DOWNLOADS / (name or url.rsplit('/', 1)[1])
    if not target.is_file():
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.part')
        failures = []
        for source in (url, *mirrors):
            print(f'fetch: downloading {source}')
            try:
                with urllib.request.urlopen(source, timeout=DOWNLOAD_TIMEOUT) as response, \
                        partial.open('wb') as stream:
                    shutil.copyfileobj(response, stream)
            except OSError as error:
                partial.unlink(missing_ok=True)
                failures.append(f'{source}: {error}')
                continue
            actual = sha256_file(partial)
            if actual == sha256:
                partial.replace(target)
                break
            partial.unlink()
            failures.append(f'{source}: its SHA-256 {actual} is not the pinned one')
        else:
            raise BuildError(f'cannot download {target.name}:\n  ' + '\n  '.join(failures) +
                             f'\nA copy from anywhere else will do: put it at {target}, where it is held to '
                             f'its pinned SHA-256 {sha256} like any download')
    actual = sha256_file(target)
    if actual != sha256:
        target.unlink()
        raise BuildError(f'{target.name}: SHA-256 {actual} is not the pinned {sha256}; '
                         'the corrupt download was removed, run again')
    return target


def extract(archive, dest):
    """Unpack a source archive whose single top-level directory becomes `dest`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='extract-', dir=dest.parent) as temporary:
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter='data')
        entries = list(Path(temporary).iterdir())
        require(len(entries) == 1 and entries[0].is_dir(),
                f'{archive.name} does not hold a single top-level directory')
        entries[0].rename(dest)
    return dest


def git_checkout(dest, url, commit, paths=(), tag=None):
    """A shallow checkout of exactly `commit`, limited to `paths` when given.

    With `tag`, the tag is fetched too: Buildroot names its version (which ends
    up in the compiler's version string) from `git describe`.
    """
    dest = Path(dest)
    if dest.is_dir():
        head = output(['git', '-C', dest, 'rev-parse', 'HEAD']).strip()
        if head == commit:
            # At the pinned commit, and as that commit has it: a file changed in the checkout is
            # built from as readily as the pinned one, and its commit would not show it.
            changed = output(['git', '-C', dest, 'status', '--porcelain', '--untracked-files=no']).splitlines()
            require(not changed, f'{dest} is at the pinned commit but {len(changed)} of its files differ from it '
                    f'({", ".join(line[3:] for line in changed[:3])}{", ..." if len(changed) > 3 else ""}): '
                    'remove the directory and it is fetched again')
            return dest
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + '.part')
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    print(f'fetch: {url} at {commit[:12]}')
    run(['git', 'init', '-q', partial])
    run(['git', '-C', partial, 'remote', 'add', 'origin', url])
    if paths:
        run(['git', '-C', partial, 'sparse-checkout', 'set', '--no-cone',
             *[f'/{path}/' for path in paths]])
    if tag:
        run(['git', '-C', partial, 'fetch', '-q', '--depth', '1', '--filter=blob:none',
             'origin', f'refs/tags/{tag}:refs/tags/{tag}'])
        run(['git', '-C', partial, 'checkout', '-q', f'refs/tags/{tag}^{{commit}}'])
    else:
        run(['git', '-C', partial, 'fetch', '-q', '--depth', '1', '--filter=blob:none',
             'origin', commit])
        run(['git', '-C', partial, 'checkout', '-q', 'FETCH_HEAD'])
    head = output(['git', '-C', partial, 'rev-parse', 'HEAD']).strip()
    require(head == commit, f'{url}: checked out {head}, not the pinned {commit}')
    partial.rename(dest)
    return dest


def buildroot():
    pin = sources()['buildroot']
    return git_checkout(SRC / 'buildroot', pin['git'], pin['commit'], tag=pin['tag'])


def openwrt():
    pin = sources()['openwrt']
    url = os.environ.get('W1700K_OPENWRT_GIT') or pin['git']
    return git_checkout(SRC / 'openwrt', url, pin['commit'], pin['paths'])


def linux_archive():
    pin = sources()['linux']
    return download(pin['url'], pin['sha256'], mirrors=pin.get('mirrors', ()))


def uboot_archive():
    pin = sources()['u-boot']
    return download(pin['url'], pin['sha256'], mirrors=pin.get('mirrors', ()))


FETCHERS = {'buildroot': buildroot, 'openwrt': openwrt, 'linux': linux_archive,
            'u-boot': uboot_archive}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('names', nargs='*', metavar='NAME',
                        help=f'sources to fetch: {", ".join(FETCHERS)} (default: all)')
    args = parser.parse_args()
    unknown = sorted(set(args.names) - set(FETCHERS))
    if unknown:
        parser.error(f'unknown source(s): {", ".join(unknown)}')
    for name in args.names or FETCHERS:
        print(f'fetch: {name}: {FETCHERS[name]()}')
    return 0


if __name__ == '__main__':
    main_guard(main)
