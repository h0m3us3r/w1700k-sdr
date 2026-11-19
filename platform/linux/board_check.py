# SPDX-License-Identifier: MIT
"""Checks of the compiled device tree and linked kernel against the board design.

A DTS or Kconfig edit that silently drops a required device, changes the NAND
layout or links OpenWrt's mtk_bmt (which rewrites a vendor BMT it fails to
parse) fails the build here instead of on a unit.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from buildlib import require
import fdt
import partitions

BOOTARGS = ('console=ttyS0,115200 earlycon panic=5 ro rdinit=/init '
            'ubi.mtd=system ubi.block=0,rootfs')
ENABLED = ('pcie0', 'pcie2', 'eth', 'gdm1', 'gdm2', 'gdm4', 'pon_pcs', 'eth_pcs',
           'switch', 'gsw_port1', 'gsw_port2', 'i2c0', 'snfi')
DISABLED = ('pcie1', 'npu', 'uart2', 'hsuart3', 'usb0', 'usb1', 'mmc0')
# (MAC, PCS, PHY label, MDIO address, reset GPIO) of the two 10 GbE ports.
TEN_GIGABIT = (('gdm2', 'pon_pcs', 'phy8', 8, 31), ('gdm4', 'eth_pcs', 'phy5', 5, 46))
# Firmware and DMA regions the SoC uses before and while Linux runs.
RESERVED = {'atf': (0x80000000, 0x200000), 'npu_binary': (0x84000000, 0xa00000),
            'qdma0_buf': (0x87000000, 0x2000000), 'qdma1_buf': (0x89000000, 0x1000000)}
# The 10 GbE PHY drivers of both board variants and their built-in firmware,
# RTL8261N (W1700K) and RTL8261CE (W1700K2), are among them, and the W25N04LV
# that some W1700K2s carry instead of the W25N04KV.
LINKED = ('airoha_probe', 'mtk_pcie_probe', 'an7581_phy_probe', 'airoha_snand_probe',
          'spinand_probe', 'ubi_attach_mtd_dev', 'ubifs_read_superblock',
          'sdr_bmt_attach', 'airoha_bmt_translate', 'airoha_bmt_transaction_recover',
          'airoha_bmt_publish', 'rtl826x_probe', 'rtl8261x_probe', 'rtl8261x_config_init',
          '_fw_rtl8261n_bin_bin', '_fw_rtl_nic_rtl8261c_bin_bin', 'w25n04lv_ooblayout_ecc')
NOT_LINKED = ('mtk_bmt_attach', 'airoha_npu_probe')


def board(dtb):
    tree = fdt.nodes(dtb)
    symbols = tree['/__symbols__']

    def node(label):
        return tree[fdt.string(symbols[label])]

    require(fdt.strings(tree['/']['compatible'])[0] == 'gemtek,w1700k-sdr',
            'DTB: wrong board compatible')
    require(fdt.string(tree['/chosen']['bootargs']) == BOOTARGS,
            'DTB: boot arguments differ from the installed layout')
    for label in ENABLED:
        require(fdt.string(node(label).get('status', b'okay\0')) == 'okay',
                f'DTB: {label} is not enabled')
    for label in DISABLED:
        require(fdt.string(node(label).get('status', b'okay\0')) == 'disabled',
                f'DTB: {label} is not disabled')
    # The low-power radio's port: disabled as built (above), and with its pins named, so that the
    # loader has only its status to change (platform/loader: efr32_give).
    require('pinctrl-0' in node('uart2') and fdt.string(symbols['uart2']) == '/soc/serial@1fbf0300',
            'DTB: uart2 has no pins, or is not where the loader enables it')
    require(fdt.string(node('gsw_port1')['label']) == 'lan3' and
            fdt.string(node('gsw_port2')['label']) == 'lan4', 'DTB: wrong switch ports')
    for mac, pcs, phy, address, reset in TEN_GIGABIT:
        require(node(mac)['phy-mode'] == b'usxgmii\0' and
                node(mac)['phy-handle'] == node(phy)['phandle'] and
                node(mac)['pcs-handle'] == node(pcs)['phandle'] and
                fdt.cells(node(phy)['reg']) == [address] and
                fdt.cells(node(phy)['reset-gpios'])[1:] == [reset, 1],
                f'DTB: 10 GbE port {mac} is not wired to PHY {address}')
    require(not any('mediatek,bmt-v2' in props or 'mediatek,nmbm' in props
                    for props in tree.values()),
            'DTB: a second bad-block manager is requested')
    found = {}
    for props in tree.values():
        if 'label' in props and 'reg' in props:
            name = fdt.string(props['label'])
            if name in partitions.GEOMETRY:
                found[name] = (tuple(fdt.cells(props['reg'])), 'read-only' in props)
    expected = {name: (extent, name in partitions.READ_ONLY['linux'])
                for name, extent in partitions.GEOMETRY.items()}
    require(found == expected, 'DTB: NAND partitions differ from platform/partitions.py')
    for name, (base, size) in RESERVED.items():
        require(fdt.cells(node(name)['reg']) == [0, base, 0, size] and 'no-map' in node(name),
                f'DTB: reserved region {name} differs')


def linked(system_map):
    names = {line.split()[-1] for line in system_map.splitlines() if line.strip()}
    missing = [name for name in LINKED if name not in names]
    require(not missing, 'kernel: not linked: ' + ', '.join(missing))
    present = [name for name in NOT_LINKED if name in names]
    require(not present, 'kernel: must not be linked: ' + ', '.join(present))
