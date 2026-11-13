# SPDX-License-Identifier: MIT
"""The loader's staged root update (update_apply in platform/loader/an7581_w1700k_sdr_env), run as
it is written: the scripts are sh-compatible, so a POSIX shell executes them here with U-Boot's
commands (ubi, ubifsload, hash, env, saveenv ...) standing in as functions over a model of the
flash: the durable filesystem as a directory, the root volume as a file, the saved environment as
a file. Each boot starts from what the last one left."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / 'platform' / 'loader' / 'an7581_w1700k_sdr_env'

# U-Boot's commands over the model in $M: durable/ (the UBIFS), rootfs (the volume), saved (the
# environment in flash: name=value lines; absent: never saved or erased), ram/ADDR, log.
HARNESS = r'''
note() { echo "$*" >> "$M/log"; }
setenv() { if [ $# -ge 2 ]; then eval "$1=\$2"; else unset "$1"; fi; }
run() { for name; do eval "$(cat "$M/env/$name")" || return 1; done; }
saveenv() {
    note saveenv
    { [ -z "${update_tried+x}" ] || echo "update_tried=$update_tried"
      [ -z "${update_failed+x}" ] || echo "update_failed=$update_failed"; } > "$M/saved"
    for name in update_new update_old update_file update_want update_got; do
        eval "[ -z \"\${$name+x}\" ]" || { echo "saved a scratch variable: $name" >> "$M/log"; }
    done
}
env() {
    case $1 in
    exists) eval "[ -n \"\${$2+x}\" ]" ;;
    erase) note "env erase"; rm -f "$M/saved" ;;
    import)
        [ "$2" = -t ] || return 1
        addr=$3; shift 4
        for name; do
            value=$(tr -d '\000' < "$M/ram/$addr" | sed -n "s/^$name=//p")
            [ -z "$value" ] || eval "$name=\$value"
        done ;;
    *) echo "env $*: not modelled" >&2; return 1 ;;
    esac
}
ubi() {
    case $1 in
    part.ro|part.rw)
        [ "$2" != durable ] || [ -d "$M/durable" ] || return 1
        attached=$2 mode=${1#part.} ;;
    write)
        [ "$attached" = system ] && [ "$mode" = rw ] && [ "$3" = rootfs ] || { echo "ubi write outside system rw" >&2; return 1; }
        note "write rootfs"
        head -c "$((0x$4))" "$M/ram/$2" > "$M/rootfs"
        if [ -e "$M/fail-write" ]; then
            rm "$M/fail-write"; printf garbage > "$M/rootfs"
        fi
        [ ! -e "$M/power-loss-after-write" ] || { rm "$M/power-loss-after-write"; note "power lost"; exit 0; } ;;
    read)
        [ "$attached" = system ] && [ "$3" = rootfs ] || return 1
        head -c "$((0x$4))" "$M/rootfs" > "$M/ram/$2" ;;
    *) echo "ubi $*: not modelled" >&2; return 1 ;;
    esac
}
ubifsmount() { [ "$attached" = durable ] && [ "$1" = ubi0:durable ] && mounted=1; }
ubifsumount() { mounted=0; }
ubifsload() {
    [ "$mounted" = 1 ] || return 1
    if [ -f "$M/durable/$2" ]; then
        cp "$M/durable/$2" "$M/ram/$1"
    else
        # As seen on a unit: this U-Boot's lookup of a name that is not there succeeds, and
        # what is loaded is the directory above it, 160 bytes of nothing. Only content counts.
        head -c 160 /dev/zero > "$M/ram/$1"
    fi
    filesize=$(printf %x "$(wc -c < "$M/ram/$1")")
}
hash() {
    [ "$1" = sha256 ] || return 1
    eval "$4=\$(head -c \"\$((0x$3))\" \"\$M/ram/$2\" | sha256sum | cut -d' ' -f1)"
}
attached=none mode=ro mounted=0
mkdir -p "$M/ram"
. "$M/defaults"
[ ! -f "$M/saved" ] || . "$M/saved"
run update_apply
note "booted $(sha256sum < "$M/rootfs" | cut -d' ' -f1)"
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


class LoaderUpdate(unittest.TestCase):
    OLD, NEW, NEWER = b'the root that runs' * 5000, b'the new root' * 9000, b'a later root' * 7000

    def setUp(self):
        self.model = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.model)
        (self.model / 'env').mkdir()
        (self.model / 'durable').mkdir()
        (self.model / 'rootfs').write_bytes(self.OLD)
        defaults = []
        for line in ENV_FILE.read_text().splitlines():
            name, _, value = line.partition('=')
            if '${' in value or ';' in value or name.startswith('update_') and ' ' in value:
                (self.model / 'env' / name).write_text(value)     # a script: run by name
            else:
                defaults.append(f"{name}='{value}'")
        (self.model / 'defaults').write_text('\n'.join(defaults) + '\n')
        (self.model / 'harness.sh').write_text(HARNESS)

    def stage(self, new, old, request=True):
        """What system-update stage leaves in durable storage."""
        staged = self.model / 'durable' / 'update'
        staged.mkdir(exist_ok=True)
        (staged / 'rootfs.new').write_bytes(new)
        (staged / 'rootfs.old').write_bytes(old)
        if request:
            (staged / 'request').write_text(f'update_new={digest(new)}\nupdate_old={digest(old)}\n')

    def settle(self):
        """What system-update confirm and cancel do."""
        shutil.rmtree(self.model / 'durable' / 'update')

    def boot(self):
        """One boot of the loader: what it logged since the last."""
        log = self.model / 'log'
        log.write_text('')
        result = subprocess.run(['sh', str(self.model / 'harness.sh')], env=dict(os.environ, M=str(self.model)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('not found', result.stderr)
        self.assertNotIn('not modelled', result.stderr)
        self.lines = log.read_text().splitlines()
        self.assertNotIn('saved a scratch variable', ' '.join(self.lines))
        return self.lines

    def root(self):
        return (self.model / 'rootfs').read_bytes()

    def saved(self):
        path = self.model / 'saved'
        return dict(line.split('=', 1) for line in path.read_text().splitlines()) if path.exists() else None

    def test_the_boot_command_applies_updates_first_and_then_boots_as_before(self):
        bootcmd = (self.model / 'env' / 'bootcmd').read_text()
        self.assertTrue(bootcmd.startswith('run update_apply; ubi part.ro ${bootpart} && ubi read'))
        self.assertTrue(bootcmd.endswith('bootm ${loadaddr}#${bootconf}'))

    def test_nothing_staged_nothing_written(self):
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.saved())
        shutil.rmtree(self.model / 'durable')                   # durable storage never formatted
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])

    def test_a_staged_root_is_written_once_and_kept_when_confirmed(self):
        self.stage(self.NEW, self.OLD)
        self.assertEqual(self.boot(), ['write rootfs', 'saveenv', f'booted {digest(self.NEW)}'])
        self.assertEqual(self.root(), self.NEW)
        self.assertEqual(self.saved(), {'update_tried': digest(self.NEW)})
        self.settle()                                           # Linux came up and confirmed
        self.assertEqual(self.boot(), ['env erase', f'booted {digest(self.NEW)}'])
        self.assertIsNone(self.saved(), 'the saved environment is gone again: the loader runs on its defaults')
        self.assertEqual(self.boot(), [f'booted {digest(self.NEW)}'], 'and nothing more is written')

    def test_a_root_never_confirmed_is_undone_by_the_next_boot(self):
        self.stage(self.NEW, self.OLD)
        self.boot()
        self.assertEqual(self.root(), self.NEW)
        self.assertEqual(self.boot(), ['write rootfs', 'saveenv', f'booted {digest(self.OLD)}'], 'the power cycle')
        self.assertEqual(self.root(), self.OLD)
        self.assertEqual(self.saved(), {'update_failed': digest(self.NEW)})
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'], 'and it is not tried again')
        self.settle()
        self.assertEqual(self.boot(), ['env erase', f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.saved())

    def test_another_root_after_a_failed_one_is_tried(self):
        self.stage(self.NEW, self.OLD)
        self.boot()
        self.boot()                                             # undone
        self.stage(self.NEWER, self.OLD)                        # staged without a boot in between
        self.assertEqual(self.boot(), ['write rootfs', 'saveenv', f'booted {digest(self.NEWER)}'])
        self.assertEqual(self.saved(), {'update_tried': digest(self.NEWER)})

    def test_a_staged_root_that_is_not_the_one_announced_is_not_written(self):
        self.stage(self.NEW, self.OLD)
        (self.model / 'durable' / 'update' / 'rootfs.new').write_bytes(self.NEW[:-1] + b'?')
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.saved())
        os.remove(self.model / 'durable' / 'update' / 'rootfs.new')
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'], 'nor one that is missing')

    def test_files_without_a_request_are_not_an_update(self):
        self.stage(self.NEW, self.OLD, request=False)           # staging was interrupted
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])

    def test_a_write_that_does_not_read_back_puts_the_previous_root_back(self):
        self.stage(self.NEW, self.OLD)
        (self.model / 'fail-write').touch()
        self.assertEqual(self.boot(), ['write rootfs', 'write rootfs', 'saveenv', f'booted {digest(self.OLD)}'])
        self.assertEqual(self.saved(), {'update_failed': digest(self.NEW)})

    def test_power_lost_between_the_write_and_its_note_writes_again(self):
        self.stage(self.NEW, self.OLD)
        (self.model / 'power-loss-after-write').touch()
        self.assertEqual(self.boot(), ['write rootfs', 'power lost'])
        self.assertIsNone(self.saved())
        self.assertEqual(self.boot(), ['write rootfs', 'saveenv', f'booted {digest(self.NEW)}'])
        self.assertEqual(self.saved(), {'update_tried': digest(self.NEW)})

    def test_power_lost_while_going_back_goes_back_again(self):
        self.stage(self.NEW, self.OLD)
        self.boot()
        (self.model / 'power-loss-after-write').touch()
        self.assertEqual(self.boot(), ['write rootfs', 'power lost'])
        self.assertEqual(self.saved(), {'update_tried': digest(self.NEW)}, 'still marked as tried')
        self.assertEqual(self.boot(), ['write rootfs', 'saveenv', f'booted {digest(self.OLD)}'])


if __name__ == '__main__':
    unittest.main()
