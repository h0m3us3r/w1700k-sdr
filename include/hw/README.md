# Hardware register reference

C headers describing every register this project knows on the W1700K: the
MediaTek MT7996 radio (host interface, MCUs, capture path, PHY), its RF A-dies
(MT7976, MT7977) and the Airoha AN7581 SoC. They are written as a datasheet:
registers the production code never touches are documented too.

The headers are plain C with no kernel or libc dependencies, so the Linux
driver, the bare-metal RISC-V firmware and host tools can all include them.

## Index

| File | Contents |
| --- | --- |
| [`hw_bits.h`](hw_bits.h) | `HW_BIT`, `HW_GENMASK`, `HW_GET`, `HW_PREP` |
| **MT7996** | |
| [`mt7996/host.h`](mt7996/host.h) | PCI IDs, BAR0, address resolution and remap windows (L1/L2/CBTOP), fixed map, ownership (LPCR), FW_STATE, reset recipe |
| [`mt7996/wfdma.h`](mt7996/wfdma.h) | Host WFDMA0: global config, prefetch, ring registers and numbering, interrupts, descriptors |
| [`mt7996/conn.h`](mt7996/conn.h) | Connectivity infrastructure: clocks, low power, hardware semaphores, A-die SPI masters, CBTOP (with the chip SKU strap), WF_TOP_MISC and its bus sleep protection, WFSYS_ON low power, the per-band WFSYS gate sequence, power-on sequence, the GCI register-field layer |
| [`mt7996/mac.h`](mt7996/mac.h) | UMAC PLE / PSE, RRO counters, packet processor, MLO, per-band LMAC words (with the channel set's fields); the WM main-task loop and host command path |
| [`mt7996/secure.h`](mt7996/secure.h) | eFuse controller, pre-authentication gates, key-hash banks, SHA engine, ECC engine |
| [`mt7996/mcu_cmd.h`](mt7996/mcu_cmd.h) | Host↔MCU protocol: TXD, legacy and UNI headers, events, download commands, UNI command and tag IDs |
| [`mt7996/fw_format.h`](mt7996/fw_format.h) | Firmware containers: RAM image, ROM patch, encryption, verification codes |
| [`mt7996/mcu_map.h`](mt7996/mcu_map.h) | WM/WA/DSP memory maps, SYSRAM (with the radar pulse buffer) and the UMAC pool, pool profiles, free areas, firmware region destinations |
| [`mt7996/mcu_irq.h`](mt7996/mcu_irq.h) | WM interrupt controller, the IRQ5 slot, software interrupts (host, WM→WA), CCIF |
| [`mt7996/mcu_dic.h`](mt7996/mcu_dic.h) | ROM diagnostic mailbox, its RAM hooks, the REG_ACCESS edit point |
| [`mt7996/mcu_periph.h`](mt7996/mcu_periph.h) | Andes core CSRs, cache control, PMA, cycle counters, timers, ROM console port, hang detector |
| [`mt7996/mcu_dma.h`](mt7996/mcu_dma.h) | MCU_DMA0 rings and descriptors, firmware ring adapter, PDA, MEM_DMA |
| [`mt7996/rom_patch.h`](mt7996/rom_patch.h) | ROM patch hook mechanism, relocation by ROM revision, our hooks, START gates |
| [`mt7996/probe_page.h`](mt7996/probe_page.h) | Our patch-SRAM mailbox: stream GO/STOP, results, the control-program interface |
| [`mt7996/dsp.h`](mt7996/dsp.h) | The three DSP cores: memories, control, triggers, interrupts |
| [`mt7996/icap.h`](mt7996/icap.h) | ICAP0/1/2 capture engines: ICAPCR0..12 |
| [`mt7996/cap_route.h`](mt7996/cap_route.h) | Capture route block: SRAM bank grants, owner, write paths, known-good routes |
| [`mt7996/dfe.h`](mt7996/dfe.h) | Per-band DFE capture mux and lane mux, capture nodes and taps |
| [`mt7996/spectrum_cmd.h`](mt7996/spectrum_cmd.h) | UNI SPECTRUM (capture) command layout and "prime" semantics |
| [`mt7996/capture_setup.h`](mt7996/capture_setup.h) | What the SPECTRUM arm leaves programmed (generated): the order of the arm, the stock request and node record, the lane-word rule |
| [`mt7996/iq_format.h`](mt7996/iq_format.h) | Sample word, beat layouts, window layout, stream chunks, 28-bit packing, export header, rates |
| [`mt7996/phy.h`](mt7996/phy.h) | PHY CR space: per-band/per-chain registers, RX gain and AGC, RX front end, RX IQ compensation filters, the TTG RX cal engines and gain-index selectors, the calibration failure log, the TX LO-leakage and TX IQ codes, the TX gain-step code table and TX IQ rows, WF_COMM, the per-band PHY init, the channel set's RX front-end words (AGC table port, RX gain offsets) and RX setup, the capture trigger block, the EEPROM bytes of the PHY init and channel set, offset names of the register-table rows of unknown role |
| [`mt7996/phy_map.h`](mt7996/phy_map.h) | PHY block map, decoded ROM register tables, the WM table programmer, the AdieSel table and its selection, the per-band init's register tables, the channel-set PHY tables A and B |
| [`mt7996/phy_init.h`](mt7996/phy_init.h) | The per-band PHY init's register tables as named lists (generated), the init's chain counts |
| [`mt7996/phy_chan.h`](mt7996/phy_chan.h) | The channel set's tables (generated): PHY tables A and B, AGC entries, gain-step tables, frequency and BW groups, the TXCAL words; the channel-set procedure's constants |
| [`mt7996/phy_cal.h`](mt7996/phy_cal.h) | Calibration items and the PHY registers each writes, the calibration init, group loops and channel set outside the calibration calls with the channel set's receive-only subset, TTG RX gain-step and tone tables, TSSI and its measurement path, the TX LO / IQ calibration engine and TX power meter, TX flatness FIR, OTFIQ, DPD LUTs and training engine, TX precompensation |
| **A-dies** | |
| [`adie/adie.h`](adie/adie.h) | Access path (SPI masters, semaphore, banks, chip selects), chip IDs, TOP registers (with the FEM pins), RF_INIT (calibration item 0), RX analog filter and trim, RX DC-offset and TX loop-DC calibration, the gain-step words, offset names of the chain-bank words of unknown role |
| [`adie/sx.h`](adie/sx.h) | Synthesizer registers common to both dies, frequency words, lock status |
| [`adie/rfmodes.h`](adie/rfmodes.h) | Chain overrides and block enables, RX input, loopback and test paths |
| [`adie/mt7976.h`](adie/mt7976.h) | MT7976 (2.4 GHz): ratios, VCO cores, presets, retune sequence, LO routing, monitor receiver |
| [`adie/mt7977.h`](adie/mt7977.h) | MT7977 (5/6 GHz): ratios, VCO range, software band search, preset and RCD tables |
| [`adie/rfinit.h`](adie/rfinit.h) | Calibration item 0's RF_INIT, RF_COMMON, XPA / XLNA and 7976C tables (generated) and their selection |
| [`adie/rfchan.h`](adie/rfchan.h) | Calibration item 1's per-channel MT7977 RF tables (generated), their key lists, column maps and constants |
| [`adie/sxchan.h`](adie/sxchan.h) | Calibration item 10, the channel set's SX tune (generated): MT7976 / MT7977 SX tables, frequency-word arithmetic, poll |
| [`adie/dcoc.h`](adie/dcoc.h) | RX DC-offset calibration as our WM runs it: the save lists and result rows (generated) |
| [`adie/adie_init.h`](adie/adie_init.h) | The FEM / antenna-switch pin mux of the PHY init: MediaTek's FEM tables (generated) and the mux's constants |
| **AN7581** | |
| [`an7581/memmap.h`](an7581/memmap.h) | Peripheral bases, GIC, DRAM, PCIe windows, interrupts, measured bandwidths |
| [`an7581/scu.h`](an7581/scu.h) | System control: PCIe clocks and resets, SerDes selects, straps, pin mux, GPIO, watchdog, UART |
| [`an7581/pcie.h`](an7581/pcie.h) | PCIe MAC subset, decode registers, ASPM and MPS facts, topology |
| [`an7581/eth.h`](an7581/eth.h) | Frame engine: GDM/CDM/PPE, QDMA and descriptors, PCS, 10G export facts |
| [`an7581/npu.h`](an7581/npu.h) | NPU cores, on-chip RAMs, boot and mailbox, bridge, the SM4 coprocessor |
| [`an7581/snfi.h`](an7581/snfi.h) | SPI-NAND controller, opcodes, vendor BBT/BMT format |
| [`an7581/w1700k_board.h`](an7581/w1700k_board.h) | W1700K board: parts, console, PCIe links, Ethernet ports and PHYs, GPIOs, flash geometry and partition maps |
| [`an7581/xr1710g_board.h`](an7581/xr1710g_board.h) | XR1710G board: what was read on a unit, and the little in which it differs from the W1700K |

## Conventions

- **Address spaces.** The MT7996 is reached through several views of the same
  buses. Each header states which view its addresses are in: a host PCIe BAR0
  offset, a connectivity bus address (`0x7xxxxxxx`), a WF bus address
  (`0x8xxxxxxx`), an MCU bus address as the WM, WA or DSP sees it, or a 12-bit
  A-die register index reached over the A-die SPI. AN7581 addresses are
  physical.
- **Names.** `<CHIP>_<BLOCK>_<REG>` for an address, `<REG>_<FIELD>` for a field
  mask, `<REG>_<FIELD>_<NAME>` for an enumerated value. Per-instance, per-band
  or per-chain registers are function-like macros. A register of unknown role
  gets an offset name (`ADIE_CHAIN_E90`, `MT7996_PHY_CSD_104(band, chain)`).
  Every name is defined once, in the header of its block;
  [`tools/check_hw_names.py`](../../tools/check_hw_names.py) reports any name
  defined twice.
- **Generated table headers** (marked in the index) hold MediaTek's tables as
  X-macro lists of (register name, value), with the constants that select and
  walk them; they include the headers that name their registers.
- **Fields** use `HW_BIT()` and `HW_GENMASK()` from [`hw_bits.h`](hw_bits.h);
  `HW_GET()` and `HW_PREP()` extract and insert field values.
- **Confidence.** Each register or field carries one tag:
  - `[measured]`: verified on hardware;
  - `[static]`: read from firmware, ROM or driver code;
  - `[inferred]`: reasoned from other facts, not yet verified.
- **`HAZARD:`** marks accesses that hang the MCU or the host, wedge a bus, need a
  reload or a power cycle, burn an eFuse, or transmit. Read these before poking
  a register by hand.
