# SPDX-License-Identifier: MIT
"""Install the SDR system on a unit running stock firmware.

    1. stop stock U-Boot at power-on and record its environment
    2. boot the write-free dumper from RAM, name the NAND part (a part never
       run on a unit stops here unless the user accepts the risk), read the
       unit's serial and back up its NAND data, as stock addresses it, to the
       host (skipped when a backup of this unit exists)
    3. build the unit's root with its SSH keys
    4. write our loader into the `loader` partition through stock, read it back
       and boot that copy
    5. from the loader: erase `system` and `durable`, create the UBI volumes,
       write and read back the system FIT, the root and the durable image
    6. make stock's bootcmd load our loader at power-on, keeping the old one as
       bootcmd_orig
    7. boot the installed system and check it

Steps 4 to 7 replace the stock firmware; the backup from step 2 is the only copy
of it. Interrupted at any point, the installation can be run again.
"""
from . import backup, config, layout, nand as parts, provision, stock, steps
from . import console as unit
from .console import StepFailed
from .session import open_session


def examine(console, log, backup_only, accept_untested):
    """At the dumper: the NAND part, checked to be one this layout supports and
    translated and, unless `backup_only`, run on a unit before (or accepted) and
    with the vendor pool stock reported at its boot; returns (part, serial)."""
    listing = backup.list_chip(console, log)
    nand = parts.identify(listing)
    stock_pool, pool = parts.pools(console.view(), listing)
    print(f'install: NAND {nand.name} (ID {nand.id.hex(" ")}), vendor pool {pool} '
          f'(stock: {stock_pool})')
    if backup_only and not nand.tested:
        print(f'install: the {nand.name} has not been run on a unit by this project; '
              'the backup writes nothing')
    elif not backup_only:
        parts.require_tested(nand, accept_untested)
    try:
        backup.check_translated(listing)
    except StepFailed as error:
        if nand.tested:
            raise
        raise StepFailed(f'{error}\n{parts.SEND_DUMP}') from None
    if not backup_only:
        parts.check_pool(nand, console.view(), listing)
    return nand, backup.read_serial(console)


def run(settings, expect_serial=None, authorized_key=None, import_host_key=None,
        force=False, backup_only=False, accept_untested=False, no_login_key=False):
    if backup_only:
        config.require_images(config.DUMPER)
    else:
        config.require_images(config.LOADER, config.DUMPER, config.SYSTEM_FIT,
                              config.ROOTFS, *config.DURABLES)
        if not no_login_key:
            provision.authorized_key(authorized_key)   # fail before touching the unit
    with open_session(settings, 'install', write=not backup_only) as session:
        console, log = session.console, session.log
        # Stock's own boot is seen, for the vendor pool it reports.
        unit.to_stock(console, log, unit.identify(console, log), fresh=True)

        environment = stock.environment(console)
        log.text('stock-environment.txt', '\n'.join(f'{key}={value}' for key, value
                                                    in sorted(environment.items())))
        installed = layout.bootcmd_extent(environment.get('bootcmd')) is not None
        if installed and not (force or backup_only):
            raise StepFailed('this unit already boots our loader; update it with '
                             'init/flash, or pass --force to install from scratch')

        print('install: booting the write-free dumper')
        stock.stage(console, log, settings, config.DUMPER, session.served)
        stock.boot(console, 'dumper')
        nand, serial = examine(console, log, backup_only, accept_untested)
        if expect_serial and serial != expect_serial:
            raise StepFailed(f'this unit is {serial}, not {expect_serial}; nothing was written')
        directory = config.unit_dir(settings.data_dir, serial)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        print(f'install: unit {serial}, directory {directory}')
        existing = directory / backup.BACKUP_NAME
        if existing.is_file():
            if backup.serial_in_backup(existing) != serial:
                raise StepFailed(f'{existing} holds another unit; move it away first')
            print(f'install: keeping the existing backup {existing}')
        else:
            print('install: backing up the NAND data')
            backup.dump(console, log, settings, session.served, directory, serial)
        calibration = backup.extract_eeprom(existing, directory)
        print(f'install: radio calibration saved to {calibration}')
        if backup_only:
            print(f'install: backup {existing}; stopping as asked (--backup-only)')
            return 0

        rootfs, host_key = provision.provision(directory, authorized_key, import_host_key,
                                             address=settings.device_ip, no_login_key=no_login_key)

        print('install: writing our loader')
        unit.to_stock(console, log, 'dumper')
        extent = steps.write_loader_slot(session)
        print('install: laying out system and durable storage')
        steps.format_system(session, config.SYSTEM_FIT, rootfs, nand)
        steps.format_durable(session, nand)

        print('install: making stock U-Boot boot our loader')
        unit.to_stock(console, log, 'loader')
        steps.save_bootcmd(session, extent)

        print('install: booting the installed system')
        status = steps.boot_installed(session)
    login = (f'Log in with: ssh root@{settings.device_ip}' if not no_login_key else
             'It has no login key: give it one with its reset button (init/key)')
    print(f'install: done; {status}. {login}\n'
          f'install: unit host key {provision.fingerprint(host_key)} '
          f'(known_hosts line in {directory / provision.KNOWN_HOSTS})\n'
          f'install: backup {existing}')
    return 0
