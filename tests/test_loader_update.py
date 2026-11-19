# SPDX-License-Identifier: MIT
"""The loader's staged updates (platform/loader/an7581_w1700k_sdr_env), run as they are written:
of the root (update_apply), and of the loader itself, which goes to a second partition that the
first loader starts (loader_next). The scripts are sh-compatible, so a POSIX shell executes them
here with U-Boot's commands (ubi, ubifsload, mtd, fdt, hash, env, saveenv, bootm ...) standing in
as functions over a model of the flash: the durable filesystem as a directory, the root volume and
the `loader-b` partition as files, the saved environment as a file, the first page of the factory
`calibration` partition as a file (absent: erased). Each boot starts from what the
last one left. The system is started as the loader starts it, one stage of `bootm` at a time,
and what is added to the device tree's /chosen on the way is kept in `chosen`."""
import hashlib
import os
import re
import shutil
import subprocess
import sys
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
    for name in update_new update_old update_file update_want update_got loader_new loader_kind key_line key_on gps_v serial_number efr32_v efr32_keep; do
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
            value=$(tr -d '\000' < "$M/ram/$addr" | LC_ALL=C sed -n "s/^$name=//p")
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
# Registers, one file each in mem/ (hex, as `mw` was given it); one that was never written reads 0.
# setexpr NAME *ADDR OP VALUE, with the result in hex and no 0x, as U-Boot leaves it.
setexpr() {
    [ $# = 4 ] || { echo "setexpr $*: not modelled" >&2; return 1; }
    case $2 in \**) seen=$(cat "$M/mem/${2#\*}" 2>/dev/null || echo 0) ;; *) echo "setexpr $*: not modelled" >&2; return 1 ;; esac
    case $3 in
    '&') eval "$1=\$(printf %x \$(( 0x$seen & $4 )))" ;;
    '|') eval "$1=\$(printf %x \$(( 0x$seen | $4 )))" ;;
    *) echo "setexpr $*: not modelled" >&2; return 1 ;;
    esac
}
# A level register (GPIO 0 to 31, GPIO 32 to 63) keeps a written bit only for a line that is an output; the
# others go on reading their pins (seen on a unit: a level written before the line was enabled
# was lost, and the line then drove what its pin had read).
mw() {
    [ $# = 2 ] || return 1
    mkdir -p "$M/mem"
    if [ "$1" = 0x1fbf0270 ] || [ "$1" = 0x1fbf0204 ]; then
        if [ "$1" = 0x1fbf0270 ]; then oe=0x1fbf0278; else oe=0x1fbf0214; fi
        was=$(cat "$M/mem/$1" 2>/dev/null || echo 0) enabled=$(cat "$M/mem/$oe" 2>/dev/null || echo 0)
        printf '%x\n' $(( (0x$2 & 0x$enabled) | (0x$was & ~0x$enabled & 0xffffffff) )) > "$M/mem/$1"
    else
        echo "$2" > "$M/mem/$1"
    fi
}
hash() {
    [ "$1" = sha256 ] || return 1
    eval "$4=\$(head -c \"\$((0x$3))\" \"\$M/ram/$2\" | sha256sum | cut -d' ' -f1)"
}
mtd() {
    # The factory identity is only ever read, a page from the start of its partition.
    if [ "$2" = calibration ]; then
        [ "$1 $4" = "read 0" ] && [ $# = 5 ] || { echo "mtd $*: not modelled" >&2; return 1; }
        note "read calibration"
        [ ! -e "$M/calibration-unreadable" ] || return 1
        if [ -f "$M/calibration" ]; then
            { cat "$M/calibration"; head -c "$((0x${5#0x}))" /dev/zero | tr '\000' '\377'; } | head -c "$((0x${5#0x}))" > "$M/ram/$3"
        else
            head -c "$((0x${5#0x}))" /dev/zero | tr '\000' '\377' > "$M/ram/$3"
        fi
        return 0
    fi
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
    set)
        # Only the system's tree takes a property, and only once it is where it will be handed over.
        [ "$stage" = fdt ] && [ "$2" = /chosen ] && [ $# = 4 ] || { echo "fdt $*: not modelled" >&2; return 1; }
        printf '%s=%s\n' "$3" "$4" >> "$M/chosen" ;;
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
    # The system, a stage at a time and in order; `go` is the boot.
    case $1 in
    start) [ "$stage" = none ] && [ "$2" = "$loadaddr#$bootconf" ] || return 1; stage=start; return 0 ;;
    loados) [ "$stage" = start ] || return 1; stage=loados; return 0 ;;
    ramdisk) [ "$stage" = loados ] || return 1; stage=ramdisk; return 0 ;;
    fdt) [ "$stage" = ramdisk ] || return 1; stage=fdt; return 0 ;;
    prep) [ "$stage" = fdt ] || return 1; stage=prep; prepared=$bootargs; return 0 ;;
    go)
        [ "$stage" = prep ] || return 1
        for name in key_line key_on gps_v serial_number efr32_v efr32_keep; do
            eval "[ -z \"\${$name+x}\" ]" || note "left a scratch variable: $name"
        done
        echo "$prepared" >> "$M/bootargs"
        note "booted $(sha256sum < "$M/rootfs" | cut -d' ' -f1)${SLOT:+ by the second loader}"
        exit 0 ;;
    esac
    [ "$1" = "$loader_b_at" ] && [ -z "$SLOT" ] || { echo "bootm $*: not modelled" >&2; return 1; }
    if [ -e "$M/second-hangs" ]; then note "the second loader hangs"; exit 0; fi
    note "the second loader starts"
    SLOT=b sh "$M/harness.sh"
    exit $?
}
attached=none mode=ro mounted=0 fdt_at= stage=none prepared=
mkdir -p "$M/ram"
rm -f "$M/chosen"
. "$M/defaults"
[ ! -f "$M/saved" ] || . "$M/saved"
[ -z "$SLOT" ] || loader_slot=$SLOT
run gps_off
run board_id
run efr32_off
run loader_next
run update_apply
run key_pass
run efr32_pass
run system_start
echo "the system was not started" >&2
exit 1
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Model(unittest.TestCase):
    """A unit's flash as the harness models it, and one boot of the loader on it."""
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

    def settle(self, cut=False):
        """What system-update confirm does, and cancel before anything was written: the request
        goes first, then the images. `cut`: the power went between the two."""
        staged = self.model / 'durable' / 'update'
        (staged / 'request').unlink(missing_ok=True)
        if not cut:
            shutil.rmtree(staged)

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
        lines = log.read_text().splitlines()
        # Every boot reads the unit's identity, once in each loader that runs; what it makes of
        # it is BoardName's business, and the rest of a boot is told without it.
        self.identity_reads = lines.count('read calibration')
        self.lines = [line for line in lines if line != 'read calibration']
        self.assertNotIn('saved a scratch variable', ' '.join(self.lines))
        return self.lines

    def root(self):
        return (self.model / 'rootfs').read_bytes()

    def saved(self):
        path = self.model / 'saved'
        return dict(line.split('=', 1) for line in path.read_text().splitlines()) if path.exists() else None

class LoaderUpdate(Model):
    def test_the_boot_command_applies_updates_first_and_then_boots_as_before(self):
        bootcmd = (self.model / 'env' / 'bootcmd').read_text()
        self.assertTrue(bootcmd.startswith('run gps_off; run board_id; run efr32_off; run loader_next; run update_apply; '
                                           'run key_pass; run efr32_pass; '
                                           'ubi part.ro ${bootpart} && ubi read'))
        self.assertTrue(bootcmd.endswith('iminfo ${loadaddr} && run system_start'))
        start = (self.model / 'env' / 'system_start').read_text()
        self.assertEqual(start, 'if bootm start ${loadaddr}#${bootconf} && bootm loados && bootm ramdisk && bootm fdt; '
                                'then run board_pass; bootm prep && bootm go; fi',
                         "the same boot, a stage at a time, with the board's name put in before the last two")

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

    def test_power_lost_while_a_confirmation_forgets_keeps_what_was_confirmed(self):
        """system-update removes the request before the images (tests/test_system_update.py). With
        the request gone the root is settled on, whatever of the images is still there."""
        self.stage(self.NEW, self.OLD)
        self.boot()
        self.settle(cut=True)
        self.assertTrue((self.model / 'durable' / 'update' / 'rootfs.old').exists())
        self.assertEqual(self.boot(), ['env erase', f'booted {digest(self.NEW)}'])
        self.assertEqual(self.root(), self.NEW)
        self.assertEqual(self.boot(), [f'booted {digest(self.NEW)}'], 'and what was left over means nothing')

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


class GpsReceiver(Model):
    """The board's GPS receiver runs from power-on unless its power line is held low, and nothing
    of ours listens to it. The loader holds it off at the start of every boot, before any root
    runs, so every root has it off: line 43 of the pin controller, driven low."""
    DATA, ENABLE, DIRECTION = '0x1fbf0270', '0x1fbf0278', '0x1fbf0260'

    def registers(self, **values):
        (self.model / 'mem').mkdir(exist_ok=True)
        for name, value in values.items():
            (self.model / 'mem' / getattr(self, name)).write_text(f'{value:x}\n')

    def register(self, name):
        return int((self.model / 'mem' / getattr(self, name)).read_text(), 16)

    def test_the_line_is_driven_low_and_no_other_line_is_touched(self):
        # As the board comes up: line 43 high (bit 11), and line 46, the PHY's reset, driven high.
        self.registers(DATA=0x00004800, ENABLE=0x00004000, DIRECTION=0x10000000)
        self.boot()
        self.assertEqual(self.register('DATA'), 0x00004000, 'bit 11 low, bit 14 as it was')
        self.assertEqual(self.register('ENABLE'), 0x00004800, 'line 43 is an output')
        self.assertEqual(self.register('DIRECTION'), 0x10400000, 'two bits a line: bit 22 is line 43')
        self.assertIn(f'booted {digest(self.OLD)}', self.lines)

    def test_it_is_done_again_at_every_boot_and_by_the_second_loader(self):
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
        self.boot()
        self.assertIn('the second loader starts', self.lines)
        self.assertEqual((self.register('DATA'), self.register('ENABLE'), self.register('DIRECTION')),
                         (0xfffff7ff, 0x800, 0x400000))
        self.registers(DATA=0xffffffff, ENABLE=0x800, DIRECTION=0x400000)      # switched on since, by its owner
        self.boot()
        self.assertEqual(self.register('DATA'), 0xfffff7ff, 'on lasts until the unit next starts')

    def test_the_line_is_made_an_output_before_its_level_is_set(self):
        """A level written to a line that is not yet an output is lost on this chip, and the line
        then drives what its pin read, which for this one is high: the receiver, held on."""
        script = (self.model / 'env' / 'gps_off').read_text()
        self.assertLess(script.index('0x1fbf0278'), script.index('0x1fbf0270'))
        self.assertLess(script.index('0x1fbf0260'), script.index('0x1fbf0270'))
        wrong = ' && '.join(sorted(script.split('; setenv')[0].split(' && '), key=lambda step: '0x1fbf0270' not in step))
        (self.model / 'env' / 'gps_off').write_text(wrong + '; setenv gps_v')
        self.registers(DATA=0x00005ff9, ENABLE=0, DIRECTION=0)                # as read on a unit at its loader
        self.boot()
        self.assertEqual(self.register('DATA') & 0x800, 0x800, 'the other order leaves the line high, as it did on a unit')

    def test_it_comes_first_and_nothing_of_it_is_saved(self):
        bootcmd = (self.model / 'env' / 'bootcmd').read_text()
        self.assertTrue(bootcmd.startswith('run gps_off; '), 'before a second loader is started or an update applied')
        self.stage(self.NEW, self.OLD)
        self.boot()
        self.assertIn('saveenv', self.lines)
        self.assertNotIn('gps', ' '.join(self.saved()))


class BoardName(GpsReceiver):
    """One set of images runs on the W1700K and on the XR1710G, and the loader tells the unit
    which it is: by the serial number in the factory identity, the first page of `calibration`,
    which it only ever reads. The name goes to the system in the device tree and on the command
    line. The XR1710G has no GPS receiver, and line 43 is a port LED there, lit when low: the
    loader puts it out again after `gps_off`, which stays first and is run on every board."""
    test_the_line_is_driven_low_and_no_other_line_is_touched = None
    test_it_is_done_again_at_every_boot_and_by_the_second_loader = None
    test_the_line_is_made_an_output_before_its_level_is_set = None
    test_it_comes_first_and_nothing_of_it_is_saved = None
    # As a unit's identity is laid out: `name=value` lines, the serial among them, then erased flash.
    IDENTITY = ('admin_password=not-a-real-one\nadmin_username=admin\nwan_mac=00:11:22:33:44:55\n'
                'lan_mac=00:11:22:33:44:56\nserial_number={serial}\nmanufacturer=Gemtek\nfan_id=0\n'
                'wifi_passphrase=nor-is-this\nwifi_wps_pin=00000000\n')
    AS_THE_BOARD_COMES_UP = dict(DATA=0x00004800, ENABLE=0x00004000, DIRECTION=0x10000000)

    def identity(self, serial=None, text=None):
        path = self.model / 'calibration'
        if serial is None and text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(text if text is not None else self.IDENTITY.format(serial=serial).encode())

    def named(self):
        """One boot: the name on the command line, which is also what went into the device tree."""
        (self.model / 'bootargs').unlink(missing_ok=True)
        self.registers(**self.AS_THE_BOARD_COMES_UP)
        self.boot()
        line, = (self.model / 'bootargs').read_text().splitlines()
        self.assertNotIn('left a scratch variable', ' '.join(self.lines))
        chosen = dict(entry.split('=', 1) for entry in (self.model / 'chosen').read_text().splitlines())
        self.assertEqual(line.count(' w1700k.board='), 1)
        self.assertTrue(line.endswith(' w1700k.board=' + chosen['w1700k,board']))
        return chosen['w1700k,board']

    def led_or_receiver_line(self):
        """Line 43: (driven as an output, its level)."""
        return bool(self.register('ENABLE') & 0x800), bool(self.register('DATA') & 0x800)

    def test_the_table_and_the_loader_tell_a_serial_the_same_way(self):
        sys.path.insert(0, str(ROOT / 'platform'))
        import boards
        for serial in ('W1700KTEST0001', 'W1700K2TEST0001', 'XR1710GTEST0001', 'XR1710G', 'W1700K', 'W1700L000001',
                       'XR1710H000001', 'XR1710', 'xr1710gtest0001', 'GTK0000000001', 'AXR1710GTEST0001'):
            with self.subTest(serial=serial):
                self.identity(serial)
                self.assertEqual(self.named(), boards.for_serial(serial))
        self.assertEqual({boards.for_serial('W1700KTEST0001'), boards.for_serial('W1700K2TEST0001')}, {'w1700k'},
                         'a serial does not tell a W1700K2 from a W1700K')
        self.assertEqual(boards.for_serial('XR1710GTEST0001'), 'xr1710g')

    def test_the_loader_knows_the_prefixes_the_table_lists(self):
        sys.path.insert(0, str(ROOT / 'platform'))
        import boards
        script = (self.model / 'env' / 'board_id').read_text()
        for board in boards.ALL:
            with self.subTest(board=board.key):
                prefix = board.serial_prefix
                beyond = prefix[:-1] + chr(ord(prefix[-1]) + 1)
                self.assertIn(f'if test "x${{serial_number}}" ">" "x{prefix}" && test "x${{serial_number}}" "<" "x{beyond}"; '
                              f'then setenv board {board.key};', script)
        self.assertTrue(script.startswith(f'setenv board {boards.UNKNOWN}; '))
        self.assertEqual(script.count('setenv board '), 1 + len(boards.ALL))

    def test_on_a_w1700k_the_receiver_stays_off(self):
        self.identity('W1700KTEST0001')
        self.assertEqual(self.named(), 'w1700k')
        self.assertEqual(self.led_or_receiver_line(), (True, False))
        self.assertEqual(self.register('DATA'), 0x00004000, 'and no other line is touched')

    def test_on_an_xr1710g_the_port_led_is_put_out_again(self):
        """`gps_off` drove the line low, which lights the LED; it is left an output, driven high,
        which is how stock holds the LED when the port has no link."""
        self.identity('XR1710GTEST0001')
        self.assertEqual(self.named(), 'xr1710g')
        self.assertEqual(self.led_or_receiver_line(), (True, True))
        self.assertEqual(self.register('DATA'), 0x00004800, 'and no other line is touched')
        self.assertEqual(self.register('DIRECTION'), 0x10400000)

    def test_a_unit_that_cannot_be_named_is_taken_for_one_with_a_receiver(self):
        """Erased flash, a partition that cannot be read, an identity with no serial, a serial of
        some other board, and bytes that are no text at all: the receiver's line stays low."""
        cases = {'erased': None,
                 'no serial': b'wan_mac=00:11:22:33:44:55\nmanufacturer=Gemtek\n',
                 'another board': self.IDENTITY.format(serial='ZZ9000TEST0001').encode(),
                 'an empty serial': b'serial_number=\nmanufacturer=Gemtek\n',
                 'a serial cut short': b'serial_number=XR1710',
                 'not text': bytes(range(1, 256)) * 8,
                 'the name inside another value': b'wifi_ssid=serial_number=XR1710GTEST0001\n'}
        for what, text in cases.items():
            with self.subTest(what=what):
                self.identity(text=text)
                self.assertEqual(self.named(), 'unknown')
                self.assertEqual(self.led_or_receiver_line(), (True, False))
        self.identity('XR1710GTEST0001')
        (self.model / 'calibration-unreadable').touch()
        self.assertEqual(self.named(), 'unknown')
        self.assertEqual(self.led_or_receiver_line(), (True, False))

    def test_the_receiver_is_switched_off_before_the_unit_is_named(self):
        bootcmd = (self.model / 'env' / 'bootcmd').read_text()
        self.assertTrue(bootcmd.startswith('run gps_off; run board_id; run efr32_off; run loader_next; '),
                        'off first, whatever the identity turns out to say; named before a second loader is started')
        self.assertNotIn('gps_off', (self.model / 'env' / 'board_id').read_text())

    def test_only_the_serial_is_taken_from_the_identity_and_it_is_not_kept(self):
        """The identity holds the unit's passwords. One variable is asked for by name, it is gone
        again before anything else runs, and nothing of it is saved or handed to the system."""
        script = (self.model / 'env' / 'board_id').read_text()
        self.assertIn('env import -t ${update_spare} 0x800 serial_number;', script)
        self.assertTrue(script.endswith('; setenv serial_number'))
        self.identity('XR1710GTEST0001')
        self.stage(self.NEW, self.OLD)
        self.assertEqual(self.named(), 'xr1710g')
        self.assertIn('saveenv', self.lines)
        self.assertNotIn('board', ' '.join(self.saved()))
        handed = (self.model / 'bootargs').read_text() + (self.model / 'chosen').read_text()
        for secret in ('XR1710GTEST0001', 'not-a-real-one', 'nor-is-this', '00:11:22:33:44:55'):
            self.assertNotIn(secret, handed)

    def test_the_identity_is_read_and_never_written(self):
        """The harness models no write to `calibration`: one would stop the boot as not modelled."""
        self.identity('XR1710GTEST0001')
        before = (self.model / 'calibration').read_bytes()
        self.named()
        self.assertEqual((self.model / 'calibration').read_bytes(), before)
        self.assertEqual(self.identity_reads, 1)

    def test_each_loader_names_the_unit_for_itself(self):
        self.identity('XR1710GTEST0001')
        (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
        self.assertEqual(self.named(), 'xr1710g')
        self.assertIn('the second loader starts', self.lines)
        self.assertEqual(self.identity_reads, 2)
        self.assertEqual(self.led_or_receiver_line(), (True, True))

    def test_every_boot_names_it_anew(self):
        self.identity('XR1710GTEST0001')
        self.assertEqual(self.named(), 'xr1710g')
        self.identity('W1700KTEST0001')
        self.assertEqual(self.named(), 'w1700k')
        self.assertEqual(self.led_or_receiver_line(), (True, False))


class LowPowerRadio(Model):
    """The W1700K's EFR32 (Bluetooth LE, Zigbee/Thread) starts its firmware at power-on unless its
    reset, line 24 of the pin controller, is held low; nothing of ours talks to it. On a unit the
    loader has named a W1700K it holds the line once the board is named, and lets it go again
    only where the owner's switch in durable storage (`efr32/switch`, written by w1700k-efr32)
    says on. What it did goes to Linux on the kernel command line. On any other board the line
    is not its to drive: the XR1710G has no such chip."""
    DATA, ENABLE, DIRECTION = '0x1fbf0204', '0x1fbf0214', '0x1fbf0220'
    BIT, DIR_BITS = 1 << 24, 3 << 16
    SERIAL = 'W1700KTEST0001'

    def setUp(self):
        super().setUp()
        self.identity(self.SERIAL)

    def identity(self, serial):
        path = self.model / 'calibration'
        if serial is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(BoardName.IDENTITY.format(serial=serial).encode())

    def registers(self, **values):
        (self.model / 'mem').mkdir(exist_ok=True)
        for name, value in values.items():
            (self.model / 'mem' / getattr(self, name)).write_text(f'{value:x}\n')

    def register(self, name):
        return int((self.model / 'mem' / getattr(self, name)).read_text(), 16)

    def switch(self, text):
        directory = self.model / 'durable' / 'efr32'
        directory.mkdir(exist_ok=True)
        (directory / 'switch').write_text(text)

    def said(self):
        handed = self.model / 'bootargs'
        handed.unlink(missing_ok=True)
        self.boot()
        self.assertNotIn('left a scratch variable', ' '.join(self.lines))
        line, = handed.read_text().splitlines()
        self.board = line.rpartition(' w1700k.board=')[2]
        return [word for word in line.split() if word.startswith('w1700k.efr32')]

    def held(self):
        return (self.register('ENABLE') & self.BIT, self.register('DIRECTION') & self.DIR_BITS,
                self.register('DATA') & self.BIT) == (self.BIT, 1 << 16, 0)

    def test_with_no_switch_the_reset_is_held_and_no_other_line_is_touched(self):
        # Line 24 an input reading high, as the chip's own pull-up leaves it; lines 17, 19, 20 and
        # 29 (the LEDs) outputs at some level, line 30 an input reading high.
        self.registers(DATA=0x611a0000, ENABLE=0x201a0000, DIRECTION=0x04000144)
        self.assertEqual(self.said(), ['w1700k.efr32=off'])
        self.assertEqual(self.board, 'w1700k')
        self.assertEqual(self.register('ENABLE'), 0x211a0000, 'line 24 is an output')
        self.assertEqual(self.register('DIRECTION'), 0x04010144, 'two bits a line from line 16: bit 16 is line 24')
        self.assertEqual(self.register('DATA'), 0x601a0000, 'bit 24 low, the rest as it was')
        self.assertIn(f'booted {digest(self.OLD)}', self.lines)

    def test_the_line_is_made_an_output_before_its_level_is_set(self):
        """As for the GPS receiver: a level written to a line that is not yet an output is lost."""
        script = (self.model / 'env' / 'efr32_off').read_text()
        self.assertLess(script.index('0x1fbf0214'), script.index('0x1fbf0204'))
        self.assertLess(script.index('0x1fbf0220'), script.index('0x1fbf0204'))
        self.assertTrue(script.endswith('; setenv efr32_v'))

    def test_it_is_held_as_soon_as_the_board_is_named_and_by_both_loaders(self):
        bootcmd = (self.model / 'env' / 'bootcmd').read_text()
        self.assertTrue(bootcmd.startswith('run gps_off; run board_id; run efr32_off; run loader_next; '),
                        'before a second loader is started, an update applied or storage read')
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
        self.assertEqual(self.said(), ['w1700k.efr32=off'])
        self.assertIn('the second loader starts', self.lines)
        self.assertTrue(self.held())

    def test_an_owner_who_keeps_it_on_has_it_let_go_at_every_boot(self):
        """Let go, not driven high: the line is an input again and the chip's pull-up ends the
        reset, as on a board nothing of ours has touched. It was held first all the same."""
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        self.switch('efr32_keep=on\n')
        for _ in range(2):
            self.assertEqual(self.said(), ['w1700k.efr32=on'])
            self.assertEqual((self.register('ENABLE') & self.BIT, self.register('DIRECTION') & self.DIR_BITS), (0, 0))
        (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
        self.assertEqual(self.said(), ['w1700k.efr32=on'], 'the second loader reads the switch as the first did')
        self.assertEqual(self.register('ENABLE') & self.BIT, 0)

    def test_switching_it_off_again_holds_it_from_the_next_boot(self):
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        self.switch('efr32_keep=on\n')
        self.assertEqual(self.said(), ['w1700k.efr32=on'])
        self.switch('efr32_keep=off\n')
        self.assertEqual(self.said(), ['w1700k.efr32=off'])
        self.assertTrue(self.held())

    def test_anything_but_a_plain_on_is_off(self):
        """A switch that cannot be read, is empty, or says something else: the radio stays held."""
        for text in ('', 'efr32_keep=\n', 'efr32_keep=1\n', 'efr32_keep=ON\n', 'keep=on\n', 'efr32_keep=on please\n'):
            with self.subTest(text=text):
                self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
                self.switch(text)
                self.assertEqual(self.said(), ['w1700k.efr32=off'])
                self.assertTrue(self.held())
        self.switch('efr32_keep=on\n')
        (self.model / 'durable').rename(self.model / 'durable.gone')
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        self.assertEqual(self.said(), ['w1700k.efr32=off'], 'storage that cannot be read')
        self.assertTrue(self.held())

    def test_nothing_of_it_is_saved_beside_an_update(self):
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        self.switch('efr32_keep=on\n')
        self.stage(self.NEW, self.OLD)
        self.assertEqual(self.said(), ['w1700k.efr32=on'])
        self.assertIn('saveenv', self.lines)
        self.assertNotIn('efr32', ' '.join(self.saved()))

    def test_a_w1700k2_is_a_w1700k(self):
        self.identity('W1700K2TEST0001')
        self.registers(DATA=0xffffffff, ENABLE=0, DIRECTION=0)
        self.assertEqual(self.said(), ['w1700k.efr32=off'])
        self.assertTrue(self.held())

    def test_on_any_other_board_the_line_is_left_alone_and_nothing_is_said(self):
        """The XR1710G has no such chip and its line 24 is not known; a unit the loader cannot
        name may be one. Neither is driven, with or without a switch in its storage, by either
        loader, and the kernel is told nothing of a radio."""
        as_found = dict(DATA=0xdfdbeef7, ENABLE=0x20000000, DIRECTION=0x04000000)     # as read on a unit at its loader
        for serial, name in (('XR1710GTEST0001', 'xr1710g'), (None, 'unknown'), ('ZZ9000TEST0001', 'unknown'),
                             ('W1700K', 'unknown')):
            for switch in (None, 'efr32_keep=on\n', 'efr32_keep=off\n'):
                for second in (False, True):
                    with self.subTest(serial=serial, switch=switch, second=second):
                        self.identity(serial)
                        shutil.rmtree(self.model / 'durable' / 'efr32', ignore_errors=True)
                        if switch:
                            self.switch(switch)
                        (self.model / 'loaderb').unlink(missing_ok=True)
                        if second:
                            (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
                        self.registers(**as_found)
                        self.assertEqual(self.said(), [])
                        self.assertEqual(self.board, name)
                        self.assertEqual({k: self.register(k) for k in as_found}, as_found)

    def test_the_table_and_the_loader_agree_on_which_boards_have_it(self):
        sys.path.insert(0, str(ROOT / 'platform'))
        import boards
        with_radio = [board for board in boards.ALL if board.efr32_reset_line is not None]
        self.assertEqual([(board.key, board.efr32_reset_line) for board in with_radio], [('w1700k', 24)])
        for name in ('efr32_off', 'efr32_pass'):
            script = (self.model / 'env' / name).read_text()
            self.assertEqual(re.findall(r'test "\$\{board\}" = "([a-z0-9]+)"', script), [board.key for board in with_radio], name)


class KeySlot(Model):
    """The paperclip key's slot (durable `key/slot`, written by w1700k-keyslot): the loader reads it
    at every boot and hands it to Linux on the kernel command line, which never opens durable
    storage to find it."""
    KEY = 'ssh-ed25519:AAAAC3NzaC1lZDI1NTE5AAAAIHRoaXMgaXMgbm90IGEgcmVhbCBrZXksIGJ1dCBvaw=='
    PLAIN = 'console=ttyS0,115200 earlycon panic=5 ro rdinit=/init ubi.mtd=system ubi.block=0,rootfs'

    def slot(self, on, key=KEY):
        directory = self.model / 'durable' / 'key'
        directory.mkdir(exist_ok=True)
        (directory / 'slot').write_text(f'key_line={key}\nkey_on={on}\n')

    def bootargs(self):
        handed = self.model / 'bootargs'
        handed.unlink(missing_ok=True)
        self.boot()
        lines = handed.read_text().splitlines()
        self.assertEqual(len(lines), 1, 'one system is booted, with one command line')
        self.assertNotIn('left a scratch variable', ' '.join(self.lines))
        # The board's name is the last word, always (BoardName); the tests here are of the rest.
        rest, _, self.board = lines[0].rpartition(' w1700k.board=')
        self.assertRegex(self.board, r'^[a-z0-9]+$')
        return rest

    def test_no_slot_is_said_to_be_empty(self):
        self.assertEqual(self.bootargs(), self.PLAIN + ' w1700k.keyslot=empty')
        (self.model / 'durable' / 'key').mkdir()
        self.assertEqual(self.bootargs(), self.PLAIN + ' w1700k.keyslot=empty', 'a directory without the file')

    def test_a_key_that_is_on_is_handed_over(self):
        self.slot(1)
        self.assertEqual(self.bootargs(), f'{self.PLAIN} w1700k.keyslot=on w1700k.key={self.KEY}')

    def test_a_key_that_is_off_stays_in_the_slot(self):
        self.slot(0)
        args = self.bootargs()
        self.assertEqual(args, self.PLAIN + ' w1700k.keyslot=off')
        self.assertNotIn('AAAA', args)

    def test_every_boot_reads_the_slot_anew(self):
        self.slot(1)
        self.assertIn('w1700k.keyslot=on', self.bootargs())
        self.slot(0)
        self.assertEqual(self.bootargs(), self.PLAIN + ' w1700k.keyslot=off')
        shutil.rmtree(self.model / 'durable' / 'key')
        self.assertEqual(self.bootargs(), self.PLAIN + ' w1700k.keyslot=empty')

    def test_the_key_is_handed_over_beside_an_update_and_never_saved(self):
        self.slot(1)
        self.stage(self.NEW, self.OLD)
        args = self.bootargs()
        self.assertIn('write rootfs', self.lines)
        self.assertIn('saveenv', self.lines)
        self.assertEqual(args, f'{self.PLAIN} w1700k.keyslot=on w1700k.key={self.KEY}')
        self.assertNotIn('key', ' '.join(self.saved()))
        self.settle()
        self.assertEqual(self.bootargs(), f'{self.PLAIN} w1700k.keyslot=on w1700k.key={self.KEY}')

    def test_storage_that_cannot_be_read_hands_over_no_key(self):
        self.slot(1)
        (self.model / 'durable').rename(self.model / 'durable.gone')
        self.assertEqual(self.bootargs(), self.PLAIN + ' w1700k.keyslot=empty')

    def test_the_second_loader_hands_it_over_when_it_boots_the_system(self):
        self.slot(1)
        (self.model / 'loaderb').write_bytes(b'FIT:W1700K SDR second ARM loader\nthe second loader\nEND\n')
        args = self.bootargs()
        self.assertIn('the second loader starts', self.lines)
        self.assertEqual(args, f'{self.PLAIN} w1700k.keyslot=on w1700k.key={self.KEY}')


if __name__ == '__main__':
    unittest.main()
