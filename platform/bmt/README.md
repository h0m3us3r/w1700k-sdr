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
replacement instead. So does a write that cannot be made in place for another
reason (into a page that is not erased, or below a page already in use): the
block is rewritten whole into a pool block and remapped, with no media failure
behind it.

## After a power cut

A replacement is a record in the journal block, the data staged in a pool
block, then the new table published below the old one and the old one given a
bad mark. What a cut leaves, at any point, is something the next start
recovers from, and every start does:

- **A whole record** is carried through or undone: the block holds the new
  image or the old one. A replacement begun for an erase is carried through
  once its candidate is erased; with a candidate that carries a bad mark or
  will not erase it is given up, as the write itself gives such a candidate up,
  and the erase is its caller's to ask again. A write is undone (the new table
  cancelled) only while no retirement of the old table was begun: the old
  table's mark may have been cut short, and with the new table gone there
  would then be none.
- **A record whose table is already in force** is finished, not played again:
  the candidate is the block's data by then and is neither verified nor
  erased. What may be left is the old table's mark. It is marked again until
  the mark is whole (a byte of it reads 0), one ticket in the journal for each
  try: a mark cut short can read as a mark one day and as none another, and
  the old table sits above the new one, so the day it read unmarked it would
  be the table again.
- **A journal that does not read back as a record** holds none. The record is
  written whole and read back before anything is done under it, and erased
  only when that is finished, so a torn journal was cut short while it was
  written, with nothing else touched, or while it was erased, with nothing
  left to do. It is erased at that start.
- **A pool block that no table maps, is not marked bad and does not read** is
  an erase or a write of ours that was cut short. It counts as free: every
  user of a free block erases it first, and one that will not erase is marked
  bad and passed over.

A page can also fail to read for no such reason, once. So nothing is taken
for torn from a single read (it is read three times), and two things are not
taken for torn at all:

- a journal that does not read while the pool holds a second table. A
  publication is under way, its record was whole when that table was written,
  and what does not read is that record, today;
- an unmarked block above the table in force that does not read. Nothing of
  ours is written there; it is an older table, and the pending record's own
  old table is the only one the recovery may mark.

Either refuses that start (read-only, below) and leaves the flash as it is for
the next one. The same goes for a whole record that is not this build's to
read (its own hash holds, its format or geometry is another) and for a table
found where the journal should be: neither is erased.

One case stays a loss. A record that reads at no try, whose work had not got
as far as the new table, is erased like a torn one, since nothing tells the
two apart. The write under it was never acknowledged; if it had already
damaged the original in place, the block has lost what it held, and the stage
that had the good copy stays out of the pool.

If the writer still cannot start (the journal's block is bad, a recovery
fails or is refused as above, the pool is not the one stock computes), the
device attaches with translation alone and nothing is written: the tables
were found and they translate, so the system starts, and what needs a write
fails and says so. The kernel log and the loader's console say `attached
READ-ONLY` and why. It used to fail the probe, which left the flash with no
reader until someone came with a serial console. A transport that fails, or
tables that are not found, still fail the probe: there is no untranslated
fallback.

Read-only is translation by the table every reader takes, stock included. It
is not a view of the data as the unfinished write meant it: a block whose
replacement was cut after its original was damaged in place reads as that
original until a start finishes the publication; the good copy is in the
stage.

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

The simulated NAND cuts the power at every action of a transaction (before
it, after it, and half-way through it) and models what a cut and a worn block
leave: erases and programs cut short that read cleanly or fail their ECC,
pages that fail to read for a while and then read, blocks that will not
erase, and bad marks cut short that show later or fade. It does not model the
W25N04KV/LV's real behaviour at a cut, which has not been measured, and
nothing here has been provoked on a unit.
