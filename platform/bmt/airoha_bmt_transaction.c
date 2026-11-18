// SPDX-License-Identifier: GPL-2.0-only
#include "airoha_bmt_transaction.h"
#if defined(AIROHA_BMT_HOST)
#include <errno.h>
#else
#include <linux/errno.h>
#endif

static int readable(int rc) { return rc >= 0 || rc == -EUCLEAN; }
/* A page whose ECC does not decode: what a write or an erase that the power cut short leaves.
 * Told apart from a read that failed (the transport's error), which is nobody's state. */
static int torn(int rc) { return rc == -EBADMSG; }
static int all_ff(const airoha_u8 *p, airoha_u32 n)
{ while (n--) if (*p++ != 255) return 0; return 1; }
#define SYSTEM(w) ((airoha_u32)(w)->map->system_blocks)
#define TOTAL(w) ((airoha_u32)(w)->map->total_blocks)
#define PAGE(w) ((airoha_u32)(w)->map->page_bytes)
#define OOB(w) ((airoha_u32)(w)->map->oob_bytes)
#define PAGES AIROHA_BMT_BLOCK_PAGES
static int free_bit(const struct airoha_bmt_transaction *w,
		const struct airoha_bmt_intent *r, airoha_u32 b)
{
	airoha_u32 i = b - SYSTEM(w);
	return b >= SYSTEM(w) && b < TOTAL(w) && ((r->free_pool[i/8] >> (i%8)) & 1);
}
static void role(struct airoha_bmt_transaction *w, airoha_u32 b, airoha_u8 value)
{ if (b >= SYSTEM(w) && b < TOTAL(w)) w->pool[b-SYSTEM(w)] = value; }

static int bad(struct airoha_bmt_transaction *w, airoha_u32 b)
{
	airoha_u8 marker[2];
	int rc = w->io.metadata.markers(w->io.metadata.ctx, b, marker);
	if (rc) return rc < 0 ? rc : -EIO;
	return marker[0] != 255 || marker[1] != 255;
}
/* One read whose ECC does not decode does not make a page torn: it is read again. What a cut
 * left fails every time; a page that only read badly once holds what it held, and to take it
 * for torn is to erase a record under which work is unfinished, or to hand out a block that
 * is a table. */
#define READ_TRIES 3
static int read_settled(struct airoha_bmt_transaction *w, airoha_u32 b, airoha_u32 p, airoha_u8 *out)
{
	int rc=0, tries;
	for (tries=0; tries<READ_TRIES; tries++) {
		rc = w->io.metadata.read_page(w->io.metadata.ctx, b, p, out);
		if (!torn(rc)) break;
	}
	return rc;
}
static int erased_settled(struct airoha_bmt_transaction *w, airoha_u32 b, airoha_u32 p)
{
	int rc=0, tries;
	for (tries=0; tries<READ_TRIES; tries++) {
		rc = w->io.metadata.erased(w->io.metadata.ctx, b, p);
		if (!torn(rc)) break;
	}
	return rc;
}
static int blank(struct airoha_bmt_transaction *w, airoha_u32 b)
{
	airoha_u32 p;
	int rc;
	for (p=0; p<PAGES; p++) {
		rc = erased_settled(w, b, p);
		if (rc != 1) return rc < 0 ? rc : 0;
	}
	return 1;
}
static int erase_checked(struct airoha_bmt_transaction *w, airoha_u32 b)
{
	int rc = w->io.metadata.erase(w->io.metadata.ctx, b);
	return rc ? (rc < 0 ? rc : -EIO) : 0;
}

static int read_image(struct airoha_bmt_transaction *w, airoha_u32 b,
		airoha_u32 key, airoha_u8 *image, airoha_u8 digest[32])
{
	airoha_u32 p, backref;
	int rc;
	for (p=0; p<PAGES; p++) {
		airoha_u8 *data = image + p*AIROHA_IMAGE_STRIDE(w->map);
		rc = w->io.read_data(w->io.metadata.ctx,b,p,data,data+PAGE(w));
		if (!readable(rc)) return rc;
		if (rc) w->corrected_reads++;
		backref = data[PAGE(w)] | (data[PAGE(w)+1] << 8);
		if (backref != 0xffff && backref != key) return -EOPNOTSUPP;
		data[PAGE(w)] = data[PAGE(w)+1] = 255;
	}
	w->io.metadata.hash(image,AIROHA_IMAGE_BYTES(w->map),digest);
	return 0;
}

static int pub_read(void *ctx, airoha_u32 b, airoha_u32 p, airoha_u8 *data)
{ struct airoha_bmt_transaction *w=ctx; return w->io.metadata.read_page(w->io.metadata.ctx,b,p,data); }
static int pub_program(void *ctx, airoha_u32 b, airoha_u32 p, const airoha_u8 *data)
{
	struct airoha_bmt_transaction *w=ctx;
	role(w,b,b==w->journal_block ? AIROHA_POOL_JOURNAL : AIROHA_POOL_TABLE);
	return w->io.metadata.program_page(w->io.metadata.ctx,b,p,data);
}
static int pub_erase(void *ctx, airoha_u32 b)
{
	struct airoha_bmt_transaction *w=ctx;
	role(w,b,AIROHA_POOL_UNKNOWN);
	return w->io.metadata.erase(w->io.metadata.ctx,b);
}
static int pub_erased(void *ctx, airoha_u32 b, airoha_u32 p)
{ struct airoha_bmt_transaction *w=ctx; return w->io.metadata.erased(w->io.metadata.ctx,b,p); }
static int pub_markers(void *ctx, airoha_u32 b, airoha_u8 out[2])
{
	struct airoha_bmt_transaction *w=ctx;
	int rc=w->io.metadata.markers(w->io.metadata.ctx,b,out);
	if (!rc && (out[0]!=255 || out[1]!=255)) role(w,b,AIROHA_POOL_BAD);
	return rc;
}
static int pub_retire(void *ctx, airoha_u32 b)
{ struct airoha_bmt_transaction *w=ctx; return w->io.metadata.retire(w->io.metadata.ctx,b); }
static int pub_verify(void *ctx, const struct airoha_bmt_intent *r)
{
	struct airoha_bmt_transaction *w=ctx;
	airoha_u8 digest[32];
	int rc=bad(w,r->candidate_block);
	if (rc) return rc < 0 ? rc : -EIO;
	rc=read_image(w,r->candidate_block,r->system_key,w->verify,digest);
	if (rc) return rc;
	return memcmp(digest,r->data_digest,32) ? -EBADMSG : 0;
}

/* A table besides the one in force: an unmarked pool block, mapped by nothing, whose first page
 * is a BMT. There is one only while a publication is under way (the new table written, the old
 * one not yet marked). 1, 0 or an error. */
static int second_table(struct airoha_bmt_transaction *w)
{
	airoha_u32 b, i;
	int rc;
	for (b=SYSTEM(w); b<TOTAL(w); b++) {
		if (b==w->bmt_block || b==w->bbt_block || b==w->journal_block) continue;
		for (i=0; i<w->map->bmt_size; i++) if (w->map->bmt_to[i]==b) break;
		if (i<w->map->bmt_size) continue;
		rc=bad(w,b);
		if (rc<0) return rc;
		if (rc) continue;
		rc=read_settled(w,b,0,w->actual);
		if (torn(rc)) continue;
		if (!readable(rc)) return rc;
		w->scratch=*w->map;
		if (!airoha_bmt_parse_bmt(&w->scratch,w->actual,PAGE(w))) return 1;
	}
	return 0;
}

/* The journal's record, if it holds one.
 *
 * A journal that does not read back as a record holds none, and that is not an error. The
 * record is written whole and read back before anything is done under it (write_journal), and
 * it is erased only once what was done under it is finished (finish). So a journal that is torn
 * was cut short either while its record was being written, when nothing else had been touched,
 * or while it was being erased, when nothing was left to do. Either way there is nothing to
 * recover. Refusing here, as this once did, refused at every later start too: the flash then
 * had no reader at all.
 *
 * What is left of it is erased now, not at the next transaction. A record whose erase was cut
 * short is one whose work is done; cells left between two states can read differently another
 * day, and such a record read back whole at some later start would be recovered against a map
 * and a block that have moved on. (This is a write at start-up. So is a recovery.)
 *
 * That argument is about a page the power cut short, and a page can also fail to read for no
 * such reason, once. So "torn" is not taken from one read (read_settled), and not against the
 * evidence: with a second table in the pool a publication is under way, its record was whole
 * when the table was written, and a journal that does not read then is a record that does not
 * read today. That start is refused (the device attaches read-only) and the journal is left for
 * the next one. What remains is a record that reads at no try while its work had not got as
 * far as the new table: it is erased, the write under it was never acknowledged, and where the
 * original had been damaged by that write the block has lost what it held.
 *
 * The block stays the journal whatever it holds: it is never counted free or handed out.
 * A whole record that is not this map's, or not this build's to read (its own hash holds: see
 * airoha_bmt_intent_decode), is refused, and so is a table found where the journal should be. */
static int load_journal(struct airoha_bmt_transaction *w)
{
	int rc=bad(w,w->journal_block);
	if (rc) return rc < 0 ? rc : -EIO;
	w->has_pending=0;
	rc=blank(w,w->journal_block);
	if (rc>0) return 0;
	if (rc<0 && !torn(rc)) return rc;
	if (!rc) {
		rc=read_settled(w,w->journal_block,0,w->actual);
		if (!torn(rc)) {
			if (!readable(rc)) return rc;
			rc=airoha_bmt_intent_decode(&w->pending,w->map,w->actual,PAGE(w),w->io.metadata.hash);
			if (!rc) {
				if (w->pending.journal_block!=w->journal_block ||
				    w->pending.bbt_block!=w->bbt_block ||
				    w->pending.logical_block<w->first || w->pending.logical_block>=w->end)
					return -EINVAL;
				rc=airoha_bmt_intent_match(&w->pending,w->map,&w->scratch,w->expected,w->io.metadata.hash);
				if (rc) return rc;
				w->has_pending=1;
				return 0;
			}
			if (rc!=-EBADMSG) return rc;
			w->scratch=*w->map;
			if (!airoha_bmt_parse_bmt(&w->scratch,w->actual,PAGE(w)) ||
			    !airoha_bmt_parse_bbt(&w->scratch,w->actual,PAGE(w)))
				return -EINVAL;
		}
	}
	rc=second_table(w);
	if (rc) return rc < 0 ? rc : -EBADMSG;
	return erase_checked(w,w->journal_block);
}

static int inventory(struct airoha_bmt_transaction *w)
{
	airoha_u32 b, i;
	int rc;
	for (b=SYSTEM(w);b<TOTAL(w);b++) {
		role(w,b,AIROHA_POOL_UNKNOWN);
		rc=bad(w,b);
		if (rc<0) return rc;
		if (rc) { role(w,b,AIROHA_POOL_BAD); continue; }
		if (b==w->journal_block) { role(w,b,AIROHA_POOL_JOURNAL); continue; }
		if (b==w->bbt_block || b==w->bmt_block) { role(w,b,AIROHA_POOL_TABLE); continue; }
		for (i=0;i<w->map->bmt_size;i++) if (w->map->bmt_to[i]==b) break;
		if (i<w->map->bmt_size) { role(w,b,AIROHA_POOL_LIVE); continue; }
		if (w->has_pending && b==w->pending.candidate_block) {
			role(w,b,AIROHA_POOL_STAGE); continue;
		}
		rc=blank(w,b);
		if (rc<0 && !torn(rc)) return rc;
		if (rc>0) { role(w,b,AIROHA_POOL_FREE); continue; }
		if (!rc) rc=read_settled(w,b,0,w->actual);
		if (torn(rc)) {
			/* A pool block that no table maps, that is not marked bad, and that
			 * does not read: an erase or a write of ours that the power cut short
			 * (a stage being cleared, a table that was not to be). It is nobody's
			 * data and stock's table search passes over a page it cannot read. It
			 * counts as free, which here means "erased before it is written":
			 * every user of a free block erases it first, and one that will not
			 * erase is marked bad (retire_data). While a record is pending, such a
			 * block of that record's is left to the recovery.
			 *
			 * Not above the table in force. Nothing of ours is written up there:
			 * a new table goes below the old one. What does not read there is an
			 * older table, and one that reads tomorrow is the table every reader
			 * then takes. The pending record's own old table is the recovery's to
			 * mark; any other refuses this start. */
			if (b>w->bmt_block) {
				if (w->has_pending && b==w->pending.old_table_block) continue;
				return -EBADMSG;
			}
			if (!(w->has_pending && free_bit(w,&w->pending,b))) role(w,b,AIROHA_POOL_FREE);
			continue;
		}
		if (!readable(rc)) return rc;
		w->scratch=*w->map;
		if (!airoha_bmt_parse_bmt(&w->scratch,w->actual,PAGE(w)) ||
		    !airoha_bmt_parse_bbt(&w->scratch,w->actual,PAGE(w)))
			role(w,b,AIROHA_POOL_TABLE);
		/* Unknown nonempty storage is quarantined, never presumed abandoned.
		 * A configured journal cannot disappear into this category. */
	}
	return 0;
}

int airoha_bmt_transaction_init(struct airoha_bmt_transaction *w,
		struct airoha_bmt *map, const struct airoha_bmt_data_io *io,
		airoha_u32 first, airoha_u32 end, airoha_u32 bbt_block,
		airoha_u32 bmt_block, airoha_u32 journal_block, airoha_u8 *work)
{
	airoha_u32 i;
	int rc;
	if (!work) return -ENOMEM;
	if (!io->read_data || !io->program_data || !io->metadata.read_page ||
	    !io->metadata.program_page || !io->metadata.erase || !io->metadata.erased ||
	    !io->metadata.markers || !io->metadata.retire || !io->metadata.hash ||
	    airoha_bmt_write_validate(map,first,end,bbt_block,bmt_block) ||
	    journal_block<map->system_blocks || journal_block>=bmt_block ||
	    journal_block==bbt_block)
		return -EINVAL;
	for(i=0;i<map->bmt_size;i++)
		if(map->bmt_to[i]==journal_block || map->bmt_to[i]>=bmt_block) return -EINVAL;
	memset(w,0,sizeof *w);
	w->map=map; w->io=*io; w->first=first; w->end=end;
	w->bbt_block=bbt_block; w->bmt_block=bmt_block; w->journal_block=journal_block;
	w->image=work; w->verify=work+AIROHA_IMAGE_BYTES(map);
	w->publisher=(struct airoha_bmt_flash){.ctx=w, .read_page=pub_read,
		.program_page=pub_program,.erase=pub_erase,.erased=pub_erased,
		.markers=pub_markers,.retire=pub_retire,.verify_image=pub_verify,
		.hash=io->metadata.hash};
	/* The vendor writes the BBT to the reserve's first good block and never
	 * moves it. A good block below it means the pool here is not the one
	 * stock computes (on the W25N04LV it is scanned from markers every
	 * boot): stock would not count that block as reserve, so nothing may
	 * be allocated there. Translation is unaffected; writing is refused. */
	for (i=map->system_blocks;i<bbt_block;i++) {
		rc=bad(w,i);
		if (rc<0) { w->failed=1; return rc; }
		if (!rc) { w->failed=1; return -EINVAL; }
	}
	rc=load_journal(w);
	if (!rc) rc=inventory(w);
	if (!rc && w->has_pending) rc=airoha_bmt_transaction_recover(w);
	if (rc) { w->failed=1; return rc; }
	w->ready=1;
	return 0;
}

/* A failed stage has never carried live data. It is erased before page zero is marked, so
 * that retries respect NAND program limits, and marked whether or not it erased: a block that
 * will not erase and is left unmarked reads as nothing, counts as free at the next start, and
 * is handed out again there. Published old destinations are retired by the BMT itself, without
 * requiring dead media to erase/program. */
static int retire_data(struct airoha_bmt_transaction *w, airoha_u32 b)
{
	int rc=bad(w,b);
	if(rc<0) return rc;
	if(rc) { role(w,b,AIROHA_POOL_BAD); return 0; }
	w->io.metadata.erase(w->io.metadata.ctx,b);
	w->io.metadata.retire(w->io.metadata.ctx,b);
	rc=bad(w,b);
	if(rc!=1) return rc<0 ? rc : -EIO;
	role(w,b,AIROHA_POOL_BAD);
	return 0;
}

/* Clear transaction-owned metadata left by an aborted publication. Only the
 * immutable initial free set is authority, never a guessed signature. */
static int cancel_metadata(struct airoha_bmt_transaction *w)
{
	airoha_u32 b;
	int rc;
	for(b=SYSTEM(w);b<w->pending.old_table_block;b++) {
		if(!free_bit(w,&w->pending,b) || b==w->journal_block ||
		   b==w->pending.candidate_block || b==w->bmt_block) continue;
		if(w->pool[b-SYSTEM(w)]==AIROHA_POOL_FREE ||
		   w->pool[b-SYSTEM(w)]==AIROHA_POOL_BAD) continue;
		rc=erase_checked(w,b);
		if(rc) {
			rc=retire_data(w,b);
			if(rc) return rc;
			continue;
		}
		role(w,b,AIROHA_POOL_FREE);
	}
	return 0;
}

/* The end of a transaction. The journal is erased last: while its record reads, a start that
 * finds it does this again, and everything before the erase can be done twice. (The stage used
 * to be erased after the journal; a cut between the two left it holding data that no record
 * named, out of the pool for good.) */
static int finish(struct airoha_bmt_transaction *w, int mapped)
{
	airoha_u32 stage=w->pending.candidate_block;
	int rc;
	if(mapped) {
		if(w->pending.old_physical>=w->bmt_block) return -EINVAL;
		/* System sources are unreachable through the new primary mapping.
		 * Old reserve destinations also have an ignored self-entry, excluded
		 * by both allocators. Do not require a dead block to erase/program. */
		role(w,w->pending.old_physical,AIROHA_POOL_LIVE);
	} else {
		rc=cancel_metadata(w);
		if(rc) return rc;
		rc=bad(w,stage);
		if(rc<0) return rc;
		if(rc) role(w,stage,AIROHA_POOL_BAD);
		else if(erase_checked(w,stage)) {
			role(w,stage,AIROHA_POOL_UNKNOWN);
			rc=retire_data(w,stage);
			if(rc) return rc;
		} else role(w,stage,AIROHA_POOL_FREE);
	}
	rc=erase_checked(w,w->journal_block);
	if(rc) return rc;
	w->has_pending=0;
	if(mapped) role(w,stage,AIROHA_POOL_LIVE);
	return 0;
}

static int publish(struct airoha_bmt_transaction *w)
{
	airoha_u32 selected;
	int rc=airoha_bmt_publish(w->map,&w->scratch,&w->pending,&w->publisher,
		w->first,w->end,w->expected,w->actual,&selected);
	if(rc) return rc;
	role(w,w->pending.old_table_block,AIROHA_POOL_BAD);
	w->bmt_block=selected; role(w,selected,AIROHA_POOL_TABLE);
	w->remaps++;
	return finish(w,1);
}

int airoha_bmt_transaction_recover(struct airoha_bmt_transaction *w)
{
	airoha_u8 digest[32];
	int current, stage_rc, rc;
	if(w->busy) return -EBUSY;
	if(!w->has_pending) return 0;
	w->busy=1;
	current=airoha_bmt_translate(w->map,w->pending.logical_block);
	if(current<0) { rc=-EINVAL; goto out; }
	if((airoha_u32)current==w->pending.candidate_block) {
		/* The table in force maps the candidate: the publication reached the flash,
		 * and the candidate is the block's data. It is not verified against the
		 * record and not erased; the publisher sees to the old table's mark, and
		 * the journal is cleared (airoha_bmt_publish). */
		rc=publish(w);
		goto out;
	}
	if(w->pending.operation==AIROHA_INTENT_ERASE) {
		/* The record is there because the original did not erase, and the way on
		 * is the one the write took: the candidate erased, then published in the
		 * original's place. An erased candidate is published whether or not the
		 * original would erase now. The publication may have begun, and one that
		 * began has to end: a table left half retired (its bad mark cut short)
		 * reads as good today and may not tomorrow, and with the new table
		 * cancelled there would then be none. A candidate that is not erased was
		 * never published from; it is erased first.
		 *
		 * A candidate that carries a bad mark, or will not erase, was never
		 * published from either (the write marks one that failed and takes the
		 * next), and cannot be: the record is given up, as the write gives it up.
		 * The erase it stood for was not acknowledged; its caller asks again. */
		rc=bad(w,w->pending.candidate_block);
		if(rc<0) goto out;
		if(!rc) {
			stage_rc=pub_verify(w,&w->pending);
			if(stage_rc) stage_rc=w->io.metadata.erase(w->io.metadata.ctx,w->pending.candidate_block);
			if(!stage_rc) { rc=publish(w); goto out; }
		}
		rc=airoha_bmt_tickets_used(&w->publisher,w->journal_block);
		if(rc<0) goto out;
		if(rc) { rc=-EIO; goto out; }
		rc=finish(w,0); if(!rc) w->aborted++;
		goto out;
	}
	stage_rc=pub_verify(w,&w->pending);
	if(!stage_rc) {
		/* Completing an unacknowledged write is allowed. This also closes a
		 * partially started metadata publication rather than abandoning it. */
		rc=publish(w);
	} else {
		rc=read_image(w,current,w->pending.system_key,w->verify,digest);
		if(rc) goto out;
		if(!memcmp(digest,w->pending.data_digest,32) ||
		   (w->pending.operation==AIROHA_INTENT_PROGRAM &&
		    !memcmp(digest,w->pending.preimage_digest,32))) {
			/* Back to the original, and the new table cancelled with it: only
			 * while the old table is untouched. A used ticket says its mark
			 * was at least begun, and a mark cut short reads as none today and
			 * may read as a mark later; with the new table cancelled there
			 * would then be no table. The record stays, this start is refused,
			 * and the next one tries the stage again. */
			rc=airoha_bmt_tickets_used(&w->publisher,w->journal_block);
			if(rc<0) goto out;
			if(rc) { rc=stage_rc; goto out; }
			rc=finish(w,0); if(!rc) w->aborted++;
		} else rc=stage_rc;
	}
	out:
	w->busy=0;
	if(rc) w->failed=1; else { w->recoveries++; w->failed=0; }
	return rc;
}

static int validate_change(struct airoha_bmt_transaction *w, const struct airoha_bmt_change *c)
{
	airoha_u32 i;
	if(c->logical<w->first || c->logical>=w->end) return -EROFS;
	if(c->offset>PAGES*PAGE(w) || c->length>PAGES*PAGE(w)-c->offset ||
	   c->oob_offset>PAGES*OOB(w) || c->oob_length>PAGES*OOB(w)-c->oob_offset ||
	   (c->length && !c->data) || (c->oob_length && !c->oob) ||
	   (c->erase && (c->length || c->oob_length))) return -EINVAL;
	for(i=0;i<c->oob_length;i++)
		if((c->oob_offset+i)%OOB(w)<2) return -EOPNOTSUPP;
	return 0;
}

static int allocate(struct airoha_bmt_transaction *w)
{
	airoha_u32 n, b, i, high, meta, pool=airoha_bmt_pool_blocks(w->map);
	for(n=0;n<pool;n++) {
		i=(w->cursor+n)%pool; b=SYSTEM(w)+i;
		if(w->pool[i]!=AIROHA_POOL_FREE || b>=w->bmt_block || b==w->journal_block) continue;
		high=b>w->journal_block ? b : w->journal_block;
		for(meta=0;meta<w->map->bmt_size;meta++)
			if(w->map->bmt_to[meta]>high) high=w->map->bmt_to[meta];
		for(meta=w->bmt_block;meta-- > high+1;)
			if(w->pool[meta-SYSTEM(w)]==AIROHA_POOL_FREE) break;
		if(meta<=high) continue;
		w->cursor=(i+1)%pool;
		return b;
	}
	return -ENOSPC;
}

static int prepare_record(struct airoha_bmt_transaction *w, const struct airoha_bmt_change *c,
		airoha_u32 stage, const airoha_u8 before[32])
{
	struct airoha_bmt_intent *r=&w->pending;
	airoha_u32 i;
	int source=airoha_bmt_translate(w->map,c->logical), shift=airoha_bmt_shift(w->map,c->logical);
	if(source<0 || shift<0) return -EINVAL;
	memset(r,0,sizeof *r);
	r->operation=c->erase ? AIROHA_INTENT_ERASE : AIROHA_INTENT_PROGRAM;
	r->journal_block=w->journal_block; r->candidate_block=stage;
	r->logical_block=c->logical; r->system_key=c->logical+shift; r->old_physical=source;
	r->old_table_block=w->bmt_block; r->bbt_block=w->bbt_block;
	r->bbt_size=w->map->bbt_size; r->bmt_size=w->map->bmt_size;
	r->old_mapping_present=(airoha_u32)source!=r->system_key;
	for(i=0;i<airoha_bmt_pool_blocks(w->map);i++) if(w->pool[i]==AIROHA_POOL_FREE)
		r->free_pool[i/8]|=1u<<(i%8);
	i=w->journal_block-SYSTEM(w); r->free_pool[i/8]|=1u<<(i%8);
	memcpy(r->preimage_digest,before,32);
	airoha_bmt_encode(w->map,w->expected,PAGE(w));
	w->io.metadata.hash(w->expected,AIROHA_BMT_BYTES,r->bmt_digest);
	airoha_bbt_encode(w->map,w->expected,PAGE(w));
	w->io.metadata.hash(w->expected,AIROHA_BBT_BYTES,r->bbt_digest);
	w->io.metadata.hash(w->image,AIROHA_IMAGE_BYTES(w->map),r->data_digest);
	return airoha_bmt_intent_encode(r,w->map,w->expected,PAGE(w),w->io.metadata.hash);
}

static int write_journal(struct airoha_bmt_transaction *w)
{
	/* Erase immediately before the WAL write. Controller status replaces a
	 * 64-page blank scan; the programmed record is read back below. */
	int rc=erase_checked(w,w->journal_block);
	if(rc) return rc;
	rc=w->io.metadata.program_page(w->io.metadata.ctx,w->journal_block,0,w->expected);
	/* A failed call can still leave a valid record. Keep it for recovery. */
	w->has_pending=1;
	if(rc) return rc<0 ? rc : -EIO;
	rc=w->io.metadata.read_page(w->io.metadata.ctx,w->journal_block,0,w->actual);
	if(!readable(rc)) return rc;
	return memcmp(w->expected,w->actual,PAGE(w)) ? -EIO : 0;
}

static int program_image_page(struct airoha_bmt_transaction *w, airoha_u32 b,
		airoha_u32 page, airoha_u32 *written, airoha_u32 *oobwritten)
{
	airoha_u8 oob[AIROHA_BMT_OOB_MAX];
	const airoha_u8 *data=w->image+page*AIROHA_IMAGE_STRIDE(w->map);
	memcpy(oob,data+PAGE(w),OOB(w));
	if(b>=SYSTEM(w)) { oob[0]=w->pending.system_key; oob[1]=w->pending.system_key>>8; }
	*written=*oobwritten=0;
	return w->io.program_data(w->io.metadata.ctx,b,page,data,oob,written,oobwritten);
}

static int stage_image(struct airoha_bmt_transaction *w)
{
	airoha_u32 p, written, oobwritten, b=w->pending.candidate_block;
	int rc=erase_checked(w,b);
	role(w,b,AIROHA_POOL_STAGE);
	if(rc) return rc;
	if(w->pending.operation==AIROHA_INTENT_PROGRAM) for(p=0;p<PAGES;p++) {
		/* Page zero supplies the vendor backreference even if its main is FF.
		 * Other entirely empty pages stay erased for subsequent appends. */
		if(p && all_ff(w->image+p*AIROHA_IMAGE_STRIDE(w->map),AIROHA_IMAGE_STRIDE(w->map)))
			continue;
		rc=program_image_page(w,b,p,&written,&oobwritten);
		if(rc || written!=PAGE(w) || oobwritten!=OOB(w)) return rc<0 ? rc : -EIO;
	}
	/* Erase status is authoritative. Programmed candidates are read back once. */
	return w->pending.operation==AIROHA_INTENT_ERASE ? 0 : pub_verify(w,&w->pending);
}

static airoha_u32 overlap(airoha_u32 a, airoha_u32 n, airoha_u32 b, airoha_u32 m)
{
	airoha_u32 first=a>b ? a : b, end=a+n<b+m ? a+n : b+m;
	return end>first ? end-first : 0;
}
static int affected(const struct airoha_bmt_transaction *w,
		const struct airoha_bmt_change *c, airoha_u32 p)
{
	return overlap(c->offset,c->length,p*PAGE(w),PAGE(w)) ||
	       overlap(c->oob_offset,c->oob_length,p*OOB(w),OOB(w));
}

static int apply_original(struct airoha_bmt_transaction *w, const struct airoha_bmt_change *c,
		struct airoha_bmt_progress *done)
{
	airoha_u32 p, written, oobwritten, b=w->pending.old_physical;
	airoha_u8 digest[32];
	int rc;
	if(c->erase) return erase_checked(w,b);
	/* Do not reprogram existing ECC, exceed partial-program budgets, or move
	 * backwards through a used block. A non-erased target page is a remap. */
	for(p=0;p<PAGES;p++) if(affected(w,c,p)) {
		rc=w->io.metadata.erased(w->io.metadata.ctx,b,p);
		if(rc!=1) return rc<0 ? rc : -EIO;
	}
	/* Even an erased early page cannot be programmed after a used later page. */
	for(p=PAGES;p-- > 0;) {
		if(affected(w,c,p)) continue;
		rc=w->io.metadata.erased(w->io.metadata.ctx,b,p);
		if(rc<0) return rc;
		if(!rc) {
			airoha_u32 q;
			for(q=0;q<p;q++) if(affected(w,c,q)) return -EIO;
			break;
		}
	}
	for(p=0;p<PAGES;p++) if(affected(w,c,p)) {
		rc=program_image_page(w,b,p,&written,&oobwritten);
		if(written>PAGE(w) || oobwritten>OOB(w)) return -EIO;
		done->data+=overlap(c->offset,c->length,p*PAGE(w),written);
		done->oob+=overlap(c->oob_offset,c->oob_length,p*OOB(w),oobwritten);
		if(rc || written!=PAGE(w) || oobwritten!=OOB(w)) return rc<0 ? rc : -EIO;
	}
	rc=read_image(w,b,w->pending.system_key,w->verify,digest);
	if(rc) return rc;
	return memcmp(digest,w->pending.data_digest,32) ? -EIO : 0;
}

int airoha_bmt_transaction_apply(struct airoha_bmt_transaction *w,
		const struct airoha_bmt_change *c, struct airoha_bmt_progress *done)
{
	airoha_u8 before[32]={0};
	airoha_u32 i, p, col, attempts;
	int source, stage, rc, started=0;
	done->data=done->oob=0;
	if(w->busy) return -EBUSY;
	if(!w->ready || w->failed || w->has_pending) return -EROFS;
	rc=validate_change(w,c); if(rc) return rc;
	if(!c->erase && !c->length && !c->oob_length) return 0;
	w->busy=1;
	source=airoha_bmt_translate(w->map,c->logical);
	if(source<0) { rc=-EINVAL; goto out; }
	if(c->erase) {
		/* Normal erase is one controller operation and no NAND WAL write.
		 * A reset-interrupted erase is idempotently retried by UBI/the caller.
		 * Only a reported media failure enters replacement publication. */
		rc=w->io.metadata.erase(w->io.metadata.ctx,source);
		if(!rc) goto out;
		if(rc!=-EIO && rc!=-EBADMSG) goto out;
		memset(w->image,255,AIROHA_IMAGE_BYTES(w->map));
		for(attempts=0;attempts<AIROHA_BMT_MAX_ENTRIES;attempts++) {
			stage=allocate(w); if(stage<0) { rc=stage; goto out; }
			rc=prepare_record(w,c,stage,before); if(rc) goto out;
			started=1; rc=write_journal(w); if(rc) goto out;
			rc=w->io.metadata.erase(w->io.metadata.ctx,stage);
			if(!rc) { rc=publish(w); goto out; }
			if(rc!=-EIO && rc!=-EBADMSG) goto out;
			rc=retire_data(w,stage); if(rc) goto out;
			rc=erase_checked(w,w->journal_block); if(rc) goto out;
			w->has_pending=0;
		}
		rc=-ENOSPC; goto out;
	}
	else {
		int shift=airoha_bmt_shift(w->map,c->logical);
		if(shift<0) { rc=-EINVAL; goto out; }
		rc=read_image(w,source,c->logical+shift,w->image,before);
		if(rc) goto out;
		for(i=0;i<c->length;i++) {
			p=(c->offset+i)/PAGE(w); col=(c->offset+i)%PAGE(w);
			w->image[p*AIROHA_IMAGE_STRIDE(w->map)+col]=c->data[i];
		}
		for(i=0;i<c->oob_length;i++) {
			p=(c->oob_offset+i)/OOB(w); col=(c->oob_offset+i)%OOB(w);
			w->image[p*AIROHA_IMAGE_STRIDE(w->map)+PAGE(w)+col]=c->oob[i];
		}
	}
	for(attempts=0;attempts<AIROHA_BMT_MAX_ENTRIES;attempts++) {
		stage=allocate(w); if(stage<0) { rc=stage; goto out; }
		rc=prepare_record(w,c,stage,before); if(rc) goto out;
		started=1;
		rc=write_journal(w); if(rc) goto out;
		rc=stage_image(w);
		if(!rc) break;
		if(rc!=-EIO && rc!=-EBADMSG) goto out;
		/* Original has not been touched. Safely retire this failed stage,
		 * then clear its journal before preparing another candidate. */
		rc=retire_data(w,stage); if(rc) goto out;
		rc=erase_checked(w,w->journal_block); if(rc) goto out;
		w->has_pending=0;
	}
	if(attempts==AIROHA_BMT_MAX_ENTRIES) { rc=-ENOSPC; goto out; }
	rc=apply_original(w,c,done);
	if(rc==-EIO || rc==-EBADMSG) rc=publish(w);
	else if(!rc) rc=finish(w,0);
	if(!rc) { done->data=c->length; done->oob=c->oob_length; }
	out:
	w->busy=0;
	if(rc && started) w->failed=1;
	return rc;
}
