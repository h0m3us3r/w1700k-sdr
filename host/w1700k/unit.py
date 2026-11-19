# SPDX-License-Identifier: MIT
"""The unit over ssh: one multiplexed connection, commands, file pushes and batched client runs.

All ssh invocations share one ControlMaster connection (socket under $XDG_RUNTIME_DIR), so a
command costs a round trip, not a handshake. The unit side is busybox sh plus the w1700k
clients in `bin_dir`.
"""
import atexit
import io
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEVICE = '/dev/w1700k-sdr'
OUT = '{out}'                     # in a Step's argv: replaced by that step's output file


class UnitError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    """One client run inside a batch. An argv element equal to OUT names its output file."""
    argv: tuple


@dataclass(frozen=True)
class StepResult:
    exit: int
    stdout: bytes
    stderr: bytes
    data: bytes                   # the output file (empty if the step wrote none)


def _private(path):
    """`path` is a directory of this user's that nobody else can write or look into."""
    try:
        found = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(found.st_mode) and found.st_uid == os.getuid() and not found.st_mode & 0o077


def _control_dir():
    """Where the ssh connection's control socket lives. Whoever can put a socket at that path is
    whom ssh then talks to in the unit's place, so the directory has to be ours alone: the
    session's runtime directory, else one under the temporary directory. That one has a name
    anybody could have made first, so it is used only if it is ours and closed to others; when it
    is not, a fresh directory is made for this process and removed with it."""
    base = os.environ.get('XDG_RUNTIME_DIR')
    if base and _private(base):
        return Path(base)
    path = Path(tempfile.gettempdir()) / f'w1700k-{os.getuid()}'
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if _private(path):
        return path
    fresh = Path(tempfile.mkdtemp(prefix='w1700k-ssh-'))
    atexit.register(shutil.rmtree, fresh, ignore_errors=True)
    return fresh


def batch_script(steps, workdir):
    """A sh script running `steps` in order, stopping at the first failure, then writing a tar of
    every step's stdout (.out), stderr (.err), exit code (.rc) and output file (.bin) to stdout."""
    lines = [f'd={workdir}', 'rm -rf "$d" && mkdir -p "$d" || exit 1', 'ok=1']
    for i, step in enumerate(steps):
        stem = f'"$d"/{i:03d}'
        argv = ' '.join(f'{stem}.bin' if a == OUT else shlex.quote(str(a)) for a in step.argv)
        lines.append(f'if [ $ok = 1 ]; then {argv} > {stem}.out 2> {stem}.err; rc=$?; '
                     f'echo $rc > {stem}.rc; [ $rc = 0 ] || ok=0; fi')
    lines.append('cd "$d" && tar cf - . ; cd /; rm -rf "$d"')
    return '\n'.join(lines) + '\n'


def parse_batch(archive, count):
    """[StepResult | None] per step from batch_script's tar; None for steps that did not run."""
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                if member.isfile():
                    files[Path(member.name).name] = tar.extractfile(member).read()
    except tarfile.TarError as exc:
        raise UnitError(f'batch returned no archive: {exc}')
    out = []
    for i in range(count):
        rc = files.get(f'{i:03d}.rc')
        if rc is None:
            out.append(None)
            continue
        out.append(StepResult(int(rc.decode().strip() or '255'), files.get(f'{i:03d}.out', b''),
                              files.get(f'{i:03d}.err', b''), files.get(f'{i:03d}.bin', b'')))
    return out


class Unit:
    def __init__(self, config):
        self.config = config
        self.target = f'{config.user}@{config.host}'
        self._control = str(_control_dir() / 'w1700k-ssh-%C')

    def tool(self, name):
        """Path of a unit-side client."""
        return f'{self.config.bin_dir}/{name}'

    def _ssh(self, command):
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=5',
                '-o', 'ControlMaster=auto', '-o', f'ControlPath={self._control}', '-o', 'ControlPersist=600']
        if self.config.ssh_key:
            argv += ['-i', str(Path(self.config.ssh_key).expanduser())]
        if self.config.known_hosts:
            argv += ['-o', f'UserKnownHostsFile={Path(self.config.known_hosts).expanduser()}',
                     '-o', 'StrictHostKeyChecking=yes']
        return argv + [self.target, command]

    def run(self, command, *, input=None, timeout=60):
        """Run a shell command on the unit: CompletedProcess with bytes stdout/stderr."""
        try:
            # Never let ssh inherit our stdin: it makes its stdin non-blocking, which would end a
            # `serve` session reading requests from the same pipe.
            stdin = {'input': input} if input is not None else {'stdin': subprocess.DEVNULL}
            result = subprocess.run(self._ssh(command), capture_output=True, timeout=timeout, **stdin)
        except subprocess.TimeoutExpired:
            raise UnitError(f'no answer from the unit in {timeout} s: {command[:120]}')
        if result.returncode == 255:
            raise UnitError(f'ssh to {self.target} failed: {result.stderr.decode(errors="replace").strip()}')
        return result

    def check(self, command, *, input=None, timeout=60):
        """Run a command that must succeed; its stdout as text."""
        result = self.run(command, input=input, timeout=timeout)
        if result.returncode:
            text = (result.stderr or result.stdout).decode(errors='replace').strip()
            raise UnitError(f'unit command failed ({result.returncode}): {text[-400:]}')
        return result.stdout.decode(errors='replace')

    def push(self, data, remote, mode=None):
        """Write bytes to a unit path atomically."""
        directory = shlex.quote(str(Path(remote).parent))
        path = shlex.quote(remote)
        chmod = f' && chmod {mode} {path}.part' if mode else ''
        self.check(f'mkdir -p {directory} && cat > {path}.part{chmod} && mv {path}.part {path}',
                   input=data, timeout=120)

    def read(self, remote, missing_ok=False):
        """A unit file's bytes (b'' for a missing file when missing_ok)."""
        path = shlex.quote(remote)
        result = self.run(f'cat {path}' if not missing_ok else f'cat {path} 2>/dev/null; true', timeout=60)
        if result.returncode:
            raise UnitError(f'cannot read {remote}')
        return result.stdout

    def batch(self, steps, timeout):
        """Run client steps in one round trip; [StepResult | None] per step (None: not reached)."""
        script = batch_script(steps, '/tmp/w1700k-batch-$$')
        result = self.run('sh -s', input=script.encode(), timeout=timeout)
        return parse_batch(result.stdout, len(steps))

    def popen(self, command):
        """A long-lived unit command with line-buffered text stdin/stdout."""
        return subprocess.Popen(self._ssh(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def popen_bytes(self, command):
        """A long-lived unit command joined byte for byte: unbuffered binary stdin and stdout."""
        return subprocess.Popen(self._ssh(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)

    def close(self):
        """End the shared connection."""
        subprocess.run(['ssh', '-o', f'ControlPath={self._control}', '-O', 'exit', self.target], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=10)
