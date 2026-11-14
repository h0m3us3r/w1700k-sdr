# SPDX-License-Identifier: MIT
"""The loader's staged updates (platform/loader/an7581_w1700k_sdr_env), run as they are written:
of the root (update_apply), and of the loader itself, which goes to a second partition that the
first loader starts (loader_next). The scripts are sh-compatible, so a POSIX shell executes them
here with U-Boot's commands (ubi, ubifsload, mtd, fdt, hash, env, saveenv, bootm ...) standing in
as functions over a model of the flash: the durable filesystem as a directory, the root volume and
the `loader-b` partition as files, the saved environment as a file. Each boot starts from what the
last one left."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / 'platform' / 'loader' / 'an7581_w1700k_sdr_env'

# U-Boot's commands over the model in $M: durable/ (the UBIFS), rootfs (the volume), loaderb (the
# `loader-b` partition; absent: erased), saved (the environment in flash: name=value lines;
# absent: never saved or erased), ram/ADDR, log. A loader image is "FIT:<description>", a line of
# payload and "END": whole when the last line is there. $SLOT is b in the second loader, which is
# this same script started again by `bootm`, as the second loader is the same build.
HARNESS = r'''
note() { echo "$*" >> "$M/log"; }
setenv() { if [ $# -ge 2 ]; then eval "$1=\$2"; else unset "$1"; fi; }
run() { for name; do eval "$(cat "$M/env/$name")" || return 1; done; }
saveenv() {
    note saveenv
    for name in update_tried update_failed loader_tried loader_failed loader_booted; do
        eval "[ -z \"\${$name+x}\" ]" || eval "echo \"$name=\$$name\""
    done > "$M/saved"
    for name in update_new update_old update_file update_want update_got loader_new loader_kind; do
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
mtd() {
    [ "$2" = loader-b ] || { echo "mtd $*: not modelled" >&2; return 1; }
    case $1 in
    erase) note "erase loader-b"; rm -f "$M/loaderb" ;;
    write)
        [ "$4" = 0 ] || return 1
        note "write loader-b"
        head -c "$((0x${5#0x}))" "$M/ram/$3" > "$M/loaderb"
        if [ -e "$M/fail-loader-write" ]; then
            rm "$M/fail-loader-write"; printf 'FIT:torn' > "$M/loaderb"
        fi
        [ ! -e "$M/power-loss-after-loader-write" ] || { rm "$M/power-loss-after-loader-write"; note "power lost"; exit 0; } ;;
    read)
        if [ -f "$M/loaderb" ]; then
            if [ $# -ge 5 ]; then head -c "$((0x${5#0x}))" "$M/loaderb" > "$M/ram/$3"; else cp "$M/loaderb" "$M/ram/$3"; fi
        else
            head -c 4096 /dev/zero | tr '\000' '\377' > "$M/ram/$3"      # erased flash
        fi ;;
    *) echo "mtd $*: not modelled" >&2; return 1 ;;
    esac
}
fdt() {
    case $1 in
    addr) [ "$(head -c 4 "$M/ram/$2")" = FIT: ] && fdt_at=$2 ;;
    get)
        [ "$2 $4 $5" = "value / description" ] || { echo "fdt $*: not modelled" >&2; return 1; }
        # The names of a tree's properties are at its end: nothing is found by name in an
        # image of which only the head is in RAM (seen on a unit: FDT_ERR_NOTFOUND).
        [ "$(tail -n 1 "$M/ram/$fdt_at")" = END ] || return 1
        eval "$3=\$(head -n 1 \"\$M/ram/\$fdt_at\" | cut -c5-)" ;;
    *) echo "fdt $*: not modelled" >&2; return 1 ;;
    esac
}
iminfo() { [ "$(tail -n 1 "$M/ram/$1")" = END ]; }
bootm() {
    [ "$1" = "$loader_b_at" ] && [ -z "$SLOT" ] || { echo "bootm $*: not modelled" >&2; return 1; }
    if [ -e "$M/second-hangs" ]; then note "the second loader hangs"; exit 0; fi
    note "the second loader starts"
    SLOT=b sh "$M/harness.sh"
    exit $?
}
attached=none mode=ro mounted=0 fdt_at=
mkdir -p "$M/ram"
. "$M/defaults"
[ ! -f "$M/saved" ] || . "$M/saved"
[ -z "$SLOT" ] || loader_slot=$SLOT
run loader_next
run update_apply
note "booted $(sha256sum < "$M/rootfs" | cut -d' ' -f1)${SLOT:+ by the second loader}"
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
        environment = {k: v for k, v in os.environ.items() if k != 'SLOT'}
        result = subprocess.run(['sh', str(self.model / 'harness.sh')], env=dict(environment, M=str(self.model)),
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
        self.assertTrue(bootcmd.startswith('run loader_next; run update_apply; ubi part.ro ${bootpart} && ubi read'))
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


SECOND = 'W1700K SDR second ARM loader'


def loader_image(text, kind=SECOND):
    """Longer than the page the first loader reads to see whether anything is there."""
    return f'FIT:{kind}\n{text} {"." * 6000}\nEND\n'.encode()


class SecondLoader(LoaderUpdate):
    """The loader's own update: a second loader in `loader-b`, started by the first (which is
    never written here), kept once confirmed and removed when it was not."""
    B1, B2 = loader_image('a newer loader'), loader_image('a still newer loader')

    def stage_loader(self, image, announced=None, root=None):
        staged = self.model / 'durable' / 'update'
        staged.mkdir(exist_ok=True)
        (staged / 'loader.new').write_bytes(image)
        lines = [f'loader_new={announced or digest(image)}']
        if root:
            new, old = root
            (staged / 'rootfs.new').write_bytes(new)
            (staged / 'rootfs.old').write_bytes(old)
            lines += [f'update_new={digest(new)}', f'update_old={digest(old)}']
        (staged / 'request').write_text('\n'.join(lines) + '\n')

    def second(self):
        path = self.model / 'loaderb'
        return path.read_bytes() if path.exists() else None

    def by_second(self, root):
        return f'booted {digest(root)} by the second loader'

    def test_the_environment_and_the_build_agree(self):
        defaults = (self.model / 'defaults').read_text()
        self.assertIn("loader_slot='a'", defaults)
        self.assertIn(f"loader_b_kind='{SECOND}'", defaults)

    def test_a_staged_loader_is_written_started_and_kept_when_confirmed(self):
        self.stage_loader(self.B1)
        self.assertEqual(self.boot(), ['erase loader-b', 'write loader-b', 'saveenv', 'the second loader starts',
                                       'saveenv', self.by_second(self.OLD)])
        self.assertEqual(self.second(), self.B1)
        self.assertEqual(self.saved(), {'loader_tried': digest(self.B1), 'loader_booted': digest(self.B1)},
                         'the second loader says that it ran: what a confirmation waits for')
        self.settle()                                           # Linux came up on it and confirmed
        self.assertEqual(self.boot(), ['the second loader starts', 'env erase', self.by_second(self.OLD)])
        self.assertIsNone(self.saved())
        self.assertEqual(self.boot(), ['the second loader starts', self.by_second(self.OLD)],
                         'from then on: started at every boot, nothing written')
        self.assertEqual(self.second(), self.B1)

    def test_a_loader_never_confirmed_is_removed_by_the_next_boot(self):
        self.stage_loader(self.B1)
        self.boot()
        self.assertEqual(self.boot(), ['erase loader-b', 'saveenv', f'booted {digest(self.OLD)}'],
                         'the first loader, alone again')
        self.assertIsNone(self.second())
        self.assertEqual(self.saved(), {'loader_failed': digest(self.B1)})
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'], 'and it is not tried again')
        self.settle()
        self.assertEqual(self.boot(), ['env erase', f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.saved())

    def test_a_second_loader_that_hangs_is_removed_after_the_power_cycle(self):
        self.stage_loader(self.B1)
        (self.model / 'second-hangs').touch()
        self.assertEqual(self.boot(), ['erase loader-b', 'write loader-b', 'saveenv', 'the second loader hangs'])
        self.assertEqual(self.saved(), {'loader_tried': digest(self.B1)}, 'it never said that it ran')
        self.assertEqual(self.boot(), ['erase loader-b', 'saveenv', f'booted {digest(self.OLD)}'])
        self.assertEqual(self.saved(), {'loader_failed': digest(self.B1)})

    def test_another_loader_after_a_failed_one_is_tried(self):
        self.stage_loader(self.B1)
        self.boot()
        self.boot()                                             # removed
        self.stage_loader(self.B2)
        self.assertEqual(self.boot()[-1], self.by_second(self.OLD))
        self.assertEqual(self.second(), self.B2)
        self.assertEqual(self.saved(), {'loader_tried': digest(self.B2), 'loader_booted': digest(self.B2)})

    def test_power_lost_between_the_write_and_its_note_writes_again(self):
        self.stage_loader(self.B1)
        (self.model / 'power-loss-after-loader-write').touch()
        self.assertEqual(self.boot(), ['erase loader-b', 'write loader-b', 'power lost'])
        self.assertIsNone(self.saved())
        self.assertEqual(self.boot()[:3], ['erase loader-b', 'write loader-b', 'saveenv'])
        self.assertEqual(self.lines[-1], self.by_second(self.OLD))

    def test_a_write_that_does_not_read_back_leaves_no_second_loader(self):
        self.stage_loader(self.B1)
        (self.model / 'fail-loader-write').touch()
        self.assertEqual(self.boot(), ['erase loader-b', 'write loader-b', 'erase loader-b',
                                       f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.second())
        self.assertIsNone(self.saved())

    def test_what_is_not_the_announced_second_loader_is_not_written(self):
        self.stage_loader(self.B1, announced=digest(self.B2))   # not the one announced
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        first = loader_image('the loader stock boots', 'W1700K SDR persistent ARM loader, staged-update 1')
        self.stage_loader(first)                                # whole, but a first loader
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        self.stage_loader(b'not an image at all')
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.second())
        self.assertIsNone(self.saved())

    def test_a_damaged_or_foreign_second_loader_is_not_started(self):
        (self.model / 'loaderb').write_bytes(self.B1[:-4])       # the end is missing
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        (self.model / 'loaderb').write_bytes(loader_image('stock firmware, or a first loader', 'something else'))
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])
        (self.model / 'loaderb').write_bytes(b'\x01\x00\x64\xaa what stock left there')
        self.assertEqual(self.boot(), [f'booted {digest(self.OLD)}'])

    def test_a_loader_and_a_root_staged_together_are_both_applied(self):
        self.stage_loader(self.B1, root=(self.NEW, self.OLD))
        self.assertEqual(self.boot(), ['erase loader-b', 'write loader-b', 'saveenv', 'the second loader starts',
                                       'saveenv', 'write rootfs', 'saveenv', self.by_second(self.NEW)],
                         'the new loader writes the new root')
        self.assertEqual(self.saved(), {'loader_tried': digest(self.B1), 'loader_booted': digest(self.B1),
                                        'update_tried': digest(self.NEW)})
        self.settle()
        self.assertEqual(self.boot(), ['the second loader starts', 'env erase', self.by_second(self.NEW)])
        self.assertIsNone(self.saved())

    def test_a_loader_and_a_root_never_confirmed_are_both_undone(self):
        self.stage_loader(self.B1, root=(self.NEW, self.OLD))
        self.boot()
        self.assertEqual(self.boot(), ['erase loader-b', 'saveenv', 'write rootfs', 'saveenv',
                                       f'booted {digest(self.OLD)}'])
        self.assertIsNone(self.second())
        self.assertEqual(self.root(), self.OLD)
        self.assertEqual(self.saved(), {'loader_failed': digest(self.B1), 'update_failed': digest(self.NEW)})

    def test_a_root_update_does_not_disturb_a_second_loader_in_place(self):
        (self.model / 'loaderb').write_bytes(self.B1)
        self.stage(self.NEW, self.OLD)
        self.assertEqual(self.boot(), ['the second loader starts', 'write rootfs', 'saveenv', self.by_second(self.NEW)])
        self.assertEqual(self.saved(), {'update_tried': digest(self.NEW)})
        self.assertEqual(self.boot(), ['the second loader starts', 'write rootfs', 'saveenv', self.by_second(self.OLD)],
                         'never confirmed: the second loader puts the previous root back')
        self.assertEqual(self.second(), self.B1)


if __name__ == '__main__':
    unittest.main()
