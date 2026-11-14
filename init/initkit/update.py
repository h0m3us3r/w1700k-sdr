# SPDX-License-Identifier: MIT
"""Update an installed unit from the current build, whatever state it is in.

The console tells where the unit is (stock, loader, dumper, Linux, or off); the
tool brings it to the installed loader's prompt, rewrites the requested parts,
reads each one back, and boots the result. A volume that already holds the new
bytes is not rewritten, so repeating an update is cheap.
"""
from . import config, layout, provision, stock, steps
from . import console as unit
from .console import StepFailed
from .session import open_session


def detect(settings):
    """Report the console state; transmits one Ctrl-C and nothing else."""
    with open_session(settings, 'detect', passive=True) as session:
        state = unit.identify(session.console, session.log)
    print(f'flash: the unit is at: {state}')
    return 0


def run(settings, serial=None, fit=False, rootfs=False, durable=False, loader=False,
        recovery=False, boot=True, authorized_key=None, no_login_key=False):
    wanted = [config.SYSTEM_FIT] if fit else []
    wanted += [config.ROOTFS] if rootfs else []
    wanted += list(config.DURABLES) if durable else []
    wanted += [config.LOADER] if loader else []
    wanted += [config.RECOVERY_FIT] if recovery else []
    config.require_images(*wanted)
    image = None
    if rootfs:
        serial = config.pick_unit(settings.data_dir, serial)
        image, _ = provision.provision(config.unit_dir(settings.data_dir, serial),
                                       authorized_key, address=settings.device_ip, no_login_key=no_login_key)
    with open_session(settings, 'flash', write=not recovery) as session:
        console, log = session.console, session.log
        state = unit.identify(console, log)
        print(f'flash: the unit is at: {state}')
        if loader:
            update_loader(session, state)
            state = 'loader'
        unit.to_installed_loader(console, log, state)
        if recovery:
            status = steps.boot_recovery(session)
            print(f'flash: recovery system running from RAM; {status}')
            return 0
        if fit or rootfs:
            steps.attach(session, 'system')
            steps.loader_network(session)
            if fit:
                steps.write_volume(session, 'fit', config.SYSTEM_FIT, fit=True,
                                   skip_current=True)
            if rootfs:
                steps.write_volume(session, 'rootfs', image, skip_current=True)
            steps.detach(session)
        if durable:
            steps.format_durable(session, steps.loader_nand(session))
        if not boot:
            print('flash: left at the loader prompt')
            return 0
        status = steps.boot_installed(session)
    print(f'flash: done; {status}')
    return 0


def update_loader(session, state):
    """Rewrite the `loader` partition through stock, then fix bootcmd if needed."""
    console, log = session.console, session.log
    unit.to_stock(console, log, state)
    installed = layout.bootcmd_extent(stock.environment(console).get('bootcmd'))
    if installed is None:
        raise StepFailed('stock does not boot our loader; install the unit with init/install')
    extent = steps.write_loader_slot(session)
    if extent != installed:
        unit.to_stock(console, log, 'loader')
        steps.save_bootcmd(session, extent)
        unit.to_installed_loader(console, log, 'stock')
