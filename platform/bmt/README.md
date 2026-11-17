# Vendor bad-block translation and replacement

Stock U-Boot does not address this SPI-NAND directly. A factory bad-block table
(BBT) shifts logical blocks past the factory-bad ones, and a block-mapping table
(BMT) substitutes worn blocks with blocks from a reserve pool at the end of the
chip:

    logical --[BBT shift]--> physical --[BMT substitution]--> final

Our U-Boot and Linux use the same mapping, so every stage finds a partition on
the same physical blocks. The files here are compiled unchanged into both (the
build copies them into each source tree):

| File | Role |
| --- | --- |
| `airoha_bmt.c/.h` | parse and validate both tables; translate. Refuses a table that does not validate; never writes |
| `airoha_bmt_write.c/.h` | encode tables and the pending-replacement record |
| `airoha_bmt_transaction.c/.h` | replace a block: stage the data in a pool block, journal the intent in a fixed reserve block, publish the new BMT, recover an interrupted replacement at the next attach |
| `airoha_bmt_mtd_write.h` | the MTD read/write/erase paths of both bindings, shared |
| `../loader/airoha_bmt_layer.c`, `../linux/sdr_bmt_layer.c` | the U-Boot and Linux MTD bindings |

Writes are allowed only from `system` to the end of `durable` (generated into
`airoha_bmt_policy.h` from `platform/partitions.py`); the vendor partitions and
the reserve pool are written only by the engine itself. Marking a block bad from
outside returns `-EOPNOTSUPP`/`-EROFS`; a failed program or erase starts a
replacement instead.

OpenWrt's `mtk_bmt` driver is deliberately not used: it computes a different
reserve pool size, fails the vendor table checksum, and then erases and rewrites
the table.

## Tests

`tests/test_platform_bmt.py` compiles these files for the host and runs them:
translation through ctypes against an independent Python model
(`tests/platform_bmt_model.py`), the table-writing and transaction harnesses in
`tests/` against a simulated NAND with injected failures, both bindings with the
real UBI and SPI-NAND functions cut from the prepared U-Boot and Linux trees,
and the controller DMA error guards. The fixtures are synthetic.
