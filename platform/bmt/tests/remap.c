/* SPDX-License-Identifier: GPL-2.0-only */
#define TRANSACTION_MODEL
#include "publication.c"
#include "airoha_bmt_transaction.h"

static struct airoha_bmt_transaction tx;
static unsigned char work[2*T_PAGES*T_STRIDE], payload[T_BS];
static unsigned char wanted[T_PAGES*T_STRIDE], previous[T_PAGES*T_STRIDE];
static int original_failure, stage_failure, original_erase_failure, preimage_error;
static int corrected_read, reentry;
static int fail_journal_clear;
static int permanent_original_erase;
static unsigned source_block, fail_page, data_calls, original_calls;
static struct airoha_bmt_change change;

static int data_read(void *ctx,unsigned b,unsigned p,unsigned char *main,unsigned char *oob)
{
	(void)ctx;
	if(b==source_block && preimage_error) return -EBADMSG;
	if(read_page(NULL,b,p,main)) return -EBADMSG;
	if(nand[b]) for(unsigned sec=0;sec<T_SECTIONS;sec++) memcpy(oob+sec*14,nand[b]->spare[p]+sec*16+2,14);
	else memset(oob,255,T_FREE);
	return corrected_read ? 3 : 0;
}
static void logical_image(unsigned b,unsigned char *out)
{
	for(unsigned p=0;p<T_PAGES;p++) {
		assert(data_read(NULL,b,p,out+p*T_STRIDE,
		                out+p*T_STRIDE+T_PS)>=0);
		out[p*T_STRIDE+T_PS]=out[p*T_STRIDE+(T_PS+1)]=255;
	}
}
static void durable_before_original(void)
{
	struct airoha_bmt_intent r;
	unsigned char digest[32];
	assert(nand[T_SYSTEM+2]);
	assert(!airoha_bmt_intent_decode(&r,&t,nand[T_SYSTEM+2]->main[0],T_PS,hash));
	image_hash(r.candidate_block,digest);
	assert(!memcmp(digest,r.data_digest,32));
}
static int data_program(void *ctx,unsigned b,unsigned p,const unsigned char *main,
		const unsigned char *oob,unsigned *written,unsigned *oobwritten)
{
	struct block *s=storage(b);
	int error=0;
	(void)ctx;
	assert(b==65 || b>=T_SYSTEM); assert(b!=T_SYSTEM && b!=T_LAST && b!=(T_SYSTEM+2));
	if(b==source_block) { durable_before_original(); original_calls++; }
	if(reentry) {
		struct airoha_bmt_progress ignored;
		assert(airoha_bmt_transaction_apply(&tx,&change,&ignored)==-EBUSY);
	}
	note('D',b,p); before(); data_calls++;
	assert((int)p>=s->last && s->programs[p]<4);
	s->last=p; s->programs[p]++;
	if(original_failure && b==source_block && p==fail_page) { original_failure=0; error=1; }
	if(stage_failure && b!=source_block && p==fail_page) { stage_failure=0; error=1; }
	unsigned len=(torn() || error) ? (T_PS/2) : T_PS;
	unsigned olen=(torn() || error) ? 7 : T_FREE;
	if(torn() && tear_ecc) unreadable[b][p]=255;
	for(unsigned i=0;i<len;i++) { assert((s->main[p][i]&main[i])==main[i]); s->main[p][i]&=main[i]; }
	for(unsigned i=0;i<olen;i++) {
		unsigned offset=(i/14)*16+2+i%14;
		assert((s->spare[p][offset]&oob[i])==oob[i]); s->spare[p][offset]&=oob[i];
	}
	if(len==T_PS) s->spare[p][T_OS-1]=0;
	/* Model a failed original program damaging a previously acknowledged page.
	 * The durable full-block after-image must preserve that page exactly. */
	if(error && b==source_block) s->main[0][0]&=0;
	*written=len; *oobwritten=olen;
	after();
	return error ? -EIO : 0;
}
static int data_erase(void *ctx,unsigned b)
{
	if(b==(T_SYSTEM+2) && fail_journal_clear && tx.has_pending) { note('E',b,0); before(); return -EIO; }
	if(b==source_block && airoha_bmt_translate(&t,64)==(int)b) {
		assert(change.erase); original_calls++;
		/* Successful erases are intentionally direct. Only a program needs a
		 * durable after-image before touching its source. Where the journal
		 * holds anything at this point, it is a whole record: an erased journal
		 * (an earlier transaction's, finished) is no record and needs none. */
		if(nand[T_SYSTEM+2] && erased(NULL,T_SYSTEM+2,0)!=1) durable_before_original();
		if(original_erase_failure || permanent_original_erase) {
			original_erase_failure=0; before();
			for(unsigned p=0;p<T_PAGES/2;p++) { memset(storage(b)->main[p],255,T_PS); memset(storage(b)->spare[p],255,T_OS); }
			after(); return -EIO;
		}
	}
	if(b==source_block && permanent_original_erase) { before(); return -EIO; }
	return erase(ctx,b);
}
static int meta_program(void *ctx,unsigned b,unsigned p,const unsigned char *main)
{
	/* A torn initial journal can stop within its protected header. */
	if(b==(T_SYSTEM+2) && p==0 && actions+1==cut_action && cut_phase==2) {
		struct block *s=storage(b); before(); s->programs[0]++; s->last=0;
		for(unsigned i=0;i<64;i++) s->main[0][i]&=main[i];
		after();
	}
	return program_page(ctx,b,p,main);
}
static const struct airoha_bmt_data_io data_io={
	.metadata={.read_page=read_page,.program_page=meta_program,.erase=data_erase,
		.erased=erased,.markers=markers,.retire=retire,.hash=hash},
	.read_data=data_read,.program_data=data_program,
};

static int reload(void)
{
	unsigned selected=stock_table();
	assert(selected);
	t=original;
	assert(!airoha_bmt_parse_bmt(&t,storage(selected)->main[0],T_PS));
	return airoha_bmt_transaction_init(&tx,&t,&data_io,64,T_END,T_SYSTEM,selected,(T_SYSTEM+2),work);
}
static void initial(int mapped,int erasing)
{
	/* Reuse the model's independent table fixtures, but start with no WAL or
	 * staged image. The transaction must create both itself. */
	setup(mapped,0);
	free(nand[T_SYSTEM+2]); nand[T_SYSTEM+2]=NULL; free(nand[T_SYSTEM+3]); nand[T_SYSTEM+3]=NULL;
	free(nand[65]); nand[65]=NULL;
	source_block=mapped ? (T_SYSTEM+1) : 65;
	original_failure=stage_failure=original_erase_failure=preimage_error=0;
	corrected_read=reentry=fail_journal_clear=permanent_original_erase=0; data_calls=original_calls=0;
	struct block *s=storage(source_block);
	for(unsigned p=0;p<(erasing ? T_PAGES : 1u);p++) {
		for(unsigned i=0;i<T_PS;i++) s->main[p][i]=(i*3+p+19)%251;
		for(unsigned sec=0;sec<T_SECTIONS;sec++) for(unsigned i=2;i<16;i++)
			s->spare[p][sec*16+i]=(sec==0 && i<4) ? 255 : (i*5+sec*7+p)%251;
		if(mapped) { s->spare[p][2]=65; s->spare[p][3]=0; }
		s->spare[p][T_OS-1]=0; s->last=p; s->programs[p]=1;
	}
	for(unsigned i=0;i<sizeof payload;i++) payload[i]=(i*11+17)%251;
	change=(struct airoha_bmt_change){.logical=64,.offset=T_PS,.length=erasing ? 0 : (T_PAGES-1)*T_PS,
		.data=erasing ? NULL : payload,.erase=erasing};
	logical_image(source_block,previous); memcpy(wanted,previous,sizeof wanted);
	if(erasing) memset(wanted,255,sizeof wanted);
	else for(unsigned i=0;i<change.length;i++) wanted[((i+T_PS)/T_PS)*T_STRIDE+(i%T_PS)]=payload[i];
	actions=0; assert(!reload()); assert(!actions);
}
static void assert_result(int new_data)
{
	unsigned selected=stock_table(); struct airoha_bmt loaded=original;
	assert(selected && !airoha_bmt_parse_bmt(&loaded,storage(selected)->main[0],T_PS));
	int physical=airoha_bmt_translate(&loaded,64); assert(physical>=0);
	logical_image(physical,image);
	assert(!memcmp(image,new_data ? wanted : previous,sizeof wanted));
}

static void basic(void)
{
	struct airoha_bmt_progress done;
	for(int mapped=0;mapped<2;mapped++) for(int erasing=0;erasing<2;erasing++) {
		initial(mapped,erasing); corrected_read=reentry=1;
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
		assert(done.data==change.length && !done.oob && !tx.has_pending && !tx.failed);
		assert_result(1);
		assert(!reload()); assert_result(1);
	}
	for(unsigned page=1;page<=63;page+=31) {
		initial(0,0); original_failure=1; fail_page=page;
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
		assert(done.data==change.length && t.bmt_size==1 && t.bmt_from[0]==65 && tx.remaps==1);
		assert_result(1); assert(!reload()); assert_result(1);
	}
	initial(1,0); original_failure=1; fail_page=32;
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	assert(t.bmt_size==2 && t.bmt_from[0]==65 && t.bmt_to[0]!=(T_SYSTEM+1));
	assert(t.bmt_from[1]==(T_SYSTEM+1) && t.bmt_to[1]==(T_SYSTEM+1)); assert_result(1);
	initial(0,1); original_erase_failure=1;
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	for(int mapped=0;mapped<2;mapped++) {
		initial(mapped,1); permanent_original_erase=1;
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
		assert(!reload()); assert_result(1);
		if(mapped) {
			/* Stock's allocator searches every destination, including ignored
			 * reserve sources; its reader still maps only the system key. */
			assert(t.bmt_size==2 && t.bmt_from[1]==(T_SYSTEM+1) && t.bmt_to[1]==(T_SYSTEM+1));
			assert(airoha_bmt_translate(&t,(T_SYSTEM+1))==(T_SYSTEM+1));
			assert(tx.pool[1]!=AIROHA_POOL_FREE);
		}
	}
	initial(0,0); stage_failure=1; fail_page=32;
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	assert(storage(T_SYSTEM+1)->spare[0][1]!=255);
	initial(0,0); preimage_error=1;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EBADMSG && !actions && !done.data);
	preimage_error=0; assert_result(0);
	initial(0,0); change.logical=63;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EROFS && !actions);
	initial(0,0); change.length=0; change.oob=payload; change.oob_length=1; change.oob_offset=T_FREE;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EOPNOTSUPP && !actions);
	initial(0,0); change.offset=2*T_PS+17; change.length=73;
	change.oob=payload+128; change.oob_offset=2*T_FREE+2; change.oob_length=20;
	memcpy(wanted,previous,sizeof wanted);
	memcpy(wanted+2*T_STRIDE+17,payload,73);
	memcpy(wanted+2*T_STRIDE+T_PS+2,payload+128,20);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	assert(done.data==73 && done.oob==20); assert_result(1);
	/* An erased earlier page below a used page requires relocation, not a
	 * forbidden backwards page program. */
	change.offset=T_PS; change.length=T_PS; change.oob_length=0;
	memcpy(wanted+T_STRIDE,payload,T_PS);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	assert(tx.remaps==1); assert_result(1);
	initial(0,0); change.length=0; change.oob=payload;
	change.oob_offset=2*T_FREE+T_FREE-4; change.oob_length=8;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EOPNOTSUPP && !actions);
	initial(0,0); storage(T_SYSTEM+4)->main[0][0]=0x42;
	assert(!reload()); assert(tx.pool[4]==AIROHA_POOL_UNKNOWN);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	assert(storage(T_SYSTEM+4)->main[0][0]==0x42); assert_result(1);
	initial(0,0); change.offset=2*T_PS+17; change.length=1500;
	change.oob=payload+T_PS; change.oob_offset=2*T_FREE+2; change.oob_length=20;
	memcpy(wanted,previous,sizeof wanted);
	memcpy(wanted+2*T_STRIDE+17,payload,1500);
	memcpy(wanted+2*T_STRIDE+T_PS+2,payload+T_PS,20);
	original_failure=1; fail_page=2; fail_old_mark=1;	/* the publication fails at the old table's mark */
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EIO);
	/* the torn program delivers half the page: past the change on the LV */
	assert(done.data==(T_PS/2-17<1500 ? T_PS/2-17 : 1500) && done.oob==5 && tx.failed && tx.has_pending);
	unsigned prior_actions=actions;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EROFS && actions==prior_actions);
	fail_old_mark=0; assert(!reload()); assert_result(1);
	initial(0,0); fail_journal_clear=1;
	assert(airoha_bmt_transaction_apply(&tx,&change,&done)==-EIO && tx.failed && tx.has_pending);
	assert_result(1); fail_journal_clear=0; assert(!reload()); assert_result(1);
	/* A good block below the BBT means the pool is not stock's: no writer. */
	initial(0,0);
	assert(airoha_bmt_transaction_init(&tx,&t,&data_io,64,T_END,T_SYSTEM+1,T_LAST,(T_SYSTEM+2),work)==-EINVAL);
	assert(!actions && tx.failed);
	initial(0,0);
	assert(airoha_bmt_transaction_init(&tx,&t,&data_io,64,T_END,T_SYSTEM,T_LAST,(T_SYSTEM+2),NULL)==-ENOMEM);
	assert(!actions);
	storage(T_SYSTEM+2)->spare[0][0]=0;
	assert(reload()==-EIO && !actions);
	puts("full data/OOB preservation, first/middle/last failures, repeated wear, erase/stage failure and reentry: passed");
}

static void transaction_cut(void)
{
	struct airoha_bmt_progress done;
	if(!setjmp(reset_point)) { airoha_bmt_transaction_apply(&tx,&change,&done); assert(!"cut not reached"); }
}
/* What a cut leaves must be something the next start recovers from: the writer comes up, and
 * the block holds the old image or the new one. A start that refuses is a failure here. It
 * once counted as a pass ("explicit damaged-WAL refusal"), and on a unit it is no pass: the
 * flash then has no reader, at that start and at every one after it. */
static void resets(void)
{
	unsigned cuts=0, completed=0, aborted=0;
	/* torn erases clean; torn erases unreadable; those, and torn programs unreadable too */
	for(int mode=0;mode<3;mode++) for(int failed=0;failed<2;failed++) {
		struct airoha_bmt_progress done;
		garble=mode>0; tear_ecc=mode>1;
		initial(0,0); original_failure=failed; fail_page=32;
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
		unsigned total=actions;
		for(unsigned cut=1;cut<=total;cut++) for(unsigned phase=0;phase<3;phase++) {
			initial(0,0); original_failure=failed; fail_page=32;
			cut_action=cut; cut_phase=phase; transaction_cut(); cut_action=0;
			original_failure=0;
			if(reload()) {
				printf("garble=%d tear_ecc=%d failed=%d cut=%u/%u phase=%u: the writer refused to start\n",garble,tear_ecc,failed,cut,total,phase);
				assert(!"a cut left a state no start recovers from");
			}
			assert(!reload());	/* and what the recovery left starts again */
			/* with no block left out of the pool: none holds what no record names */
			for(unsigned i=0;i<T_POOL;i++) if(tx.pool[i]==AIROHA_POOL_UNKNOWN) {
				printf("garble=%d tear_ecc=%d failed=%d cut=%u/%u phase=%u: pool block %u is left unknown\n",garble,tear_ecc,failed,cut,total,phase,i);
				assert(!"a cut left a block out of the pool");
			}
			unsigned selected=stock_table(); struct airoha_bmt loaded=original;
			assert(selected && !airoha_bmt_parse_bmt(&loaded,storage(selected)->main[0],T_PS));
			logical_image(airoha_bmt_translate(&loaded,64),image);
			if(!memcmp(image,wanted,sizeof wanted)) completed++;
			else { assert(!memcmp(image,previous,sizeof previous)); aborted++; }
			/* the writer is whole again: the same change goes through */
			assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
			cuts++;
		}
	}
	garble=tear_ecc=0;
	printf("program transactions cut at every action, torn erases and programs clean and unreadable: %u cuts; new=%u old=%u; none refused, no block left out\n",cuts,completed,aborted);
}

/* An erase the original block refuses becomes a replacement: the record, the candidate erased,
 * the candidate published. Cut anywhere, the next start recovers and the erase, asked for again
 * (an erase has no old image to go back to: its caller retries it), leaves the block erased.
 *
 * And the tables stay found. A publication that began is finished by the recovery, whether or
 * not the original would erase after all: to go back, the new table would be cancelled, and
 * the old one may already carry a bad mark that was cut short. Such a mark reads as none today
 * and as a mark later (age()), and then there would be no table at all. */
static void erase_resets(void)
{
	unsigned cuts=0;
	for(int mode=0;mode<3;mode++) for(int permanent=0;permanent<2;permanent++) {
		struct airoha_bmt_progress done;
		garble=mode>0; tear_ecc=mode>1;
		initial(0,1); original_erase_failure=!permanent; permanent_original_erase=permanent;
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
		unsigned total=actions;
		for(unsigned cut=1;cut<=total;cut++) for(unsigned phase=0;phase<3;phase++) {
			initial(0,1); original_erase_failure=!permanent; permanent_original_erase=permanent;
			marginal_marks=1;
			cut_action=cut; cut_phase=phase; transaction_cut(); cut_action=0;
			original_erase_failure=0;
			if(reload()) {
				printf("garble=%d tear_ecc=%d permanent=%d cut=%u/%u phase=%u: the writer refused to start\n",garble,tear_ecc,permanent,cut,total,phase);
				assert(!"a cut left a state no start recovers from");
			}
			assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
			age();
			assert(stock_table());	/* a table is still found, */
			assert_result(1);	/* and it is the one that maps the erased block */
			marginal_marks=0;
			assert(!reload()); assert_result(1);
			cuts++;
		}
	}
	garble=tear_ecc=0; marginal_marks=0;
	for(int failed=0;failed<2;failed++) for(unsigned phase=0;phase<3;phase++) {
		struct airoha_bmt_progress done;
		/* An ordinary erase has no record before the controller's operation: cut
		 * short, it is retried. */
		initial(0,1);original_erase_failure=failed;
		cut_action=1;cut_phase=phase;transaction_cut();cut_action=0;
		original_erase_failure=0;
		assert(!reload());
		assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
		assert_result(1);cuts++;
	}
	printf("erase replacements cut at every action, with bad marks that show later: %u cuts; all recovered, a table always found\n",cuts);
}

/* ---- What a cut is not: reads that fail once, blocks that will not erase, marks that fade. ---- */
#define J (T_SYSTEM+2)
static unsigned char kept[T_PS], after_image[T_PAGES*T_STRIDE];
static void remap_program(void) { initial(0,0); original_failure=1; fail_page=32; }
static void remap_erase(void) { initial(0,1); permanent_original_erase=1; }
/* a write into a page already written: a remap with the original left whole */
static void remap_used_page(void)
{
	initial(0,0); change.offset=0; change.length=T_PS;
	memcpy(wanted,previous,sizeof wanted); memcpy(wanted,payload,T_PS);
}
/* The action at which an uncut run of `prepare` did something (its first or last such). */
static unsigned locate(void (*prepare)(void),int kind,unsigned b,unsigned p,int last)
{
	struct airoha_bmt_progress done; unsigned a;
	prepare(); assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	a=action_of(kind,b,p,last); assert(a);
	return a;
}
static void cut_at(void (*prepare)(void),unsigned action,unsigned phase)
{
	prepare(); cut_action=action; cut_phase=phase; transaction_cut(); cut_action=0;
	original_failure=0;
}
static int record_whole(void)
{
	struct airoha_bmt_intent r;
	return nand[J] && !airoha_bmt_intent_decode(&r,&original,nand[J]->main[0],T_PS,hash);
}

/* A page that does not read is not thereby torn. */
static void unstable_reads(void)
{
	struct airoha_bmt_progress done;
	unsigned mark=locate(remap_program,'M',T_LAST,0,1);
	/* The new table written, the old one not yet marked; then the old table does not read for
	 * a whole start. The table found is the new one, and the recovery has to end what the
	 * write began: marked, the old table stays out the day it reads again. (It used to be
	 * counted free and left unmarked: it then came back as the table, with the block on the
	 * damaged original.) */
	cut_at(remap_program,mark,0);
	unreadable[T_LAST][0]=200;
	assert(!reload() && tx.recoveries==1 && !tx.has_pending);
	assert(!storage(T_LAST)->spare[0][1]);
	unreadable[T_LAST][0]=0;
	assert(stock_table() && stock_table()!=T_LAST); assert_result(1);
	assert(!reload()); assert_result(1);
	/* The same cut; the journal's record does not read for a whole start. With a second
	 * table in the pool that is no torn journal: the start is refused and the record kept,
	 * and the next start finishes the write. (It used to be erased: the block then stayed on
	 * the damaged original, and the writer never started again.) */
	cut_at(remap_program,mark,0);
	unreadable[J][0]=200;
	assert(reload()); assert(record_whole());
	unreadable[J][0]=0;
	assert(!reload() && tx.recoveries==1); assert_result(1);
	/* A record that fails to read twice and then reads is simply read. */
	cut_at(remap_program,mark,0);
	unreadable[J][0]=2;
	assert(!reload() && tx.recoveries==1); assert_result(1);
	/* With no second table there is nothing to tell such a record from a torn one: it is
	 * erased, the writer starts, and the block holds what it held (the original was not
	 * touched yet). */
	unsigned staging=locate(remap_program,'D',T_SYSTEM+1,5,0);
	cut_at(remap_program,staging,1);
	unreadable[J][0]=200;
	assert(!reload() && !tx.has_pending && erased(NULL,J,0)==1); assert_result(0);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	puts("reads that fail for a while: an old table and a record that do not read during a publication: passed");
}

/* A block that will not erase is marked bad and passed over, wherever it turns up. */
static void dead_table_slot(void) { remap_program(); dead[T_LAST-1]=1; unreadable[T_LAST-1][0]=255; }
static void dead_stage(void) { initial(0,0); dead[T_SYSTEM+1]=1; unreadable[T_SYSTEM+1][0]=255; }
static void dead_erase_stage(void) { remap_erase(); dead[T_SYSTEM+1]=1; unreadable[T_SYSTEM+1][0]=255; }
static void dead_bystander(void) { initial(0,0); dead[T_SYSTEM+5]=1; unreadable[T_SYSTEM+5][0]=255; }
static void flaky_erase_stage(void) { remap_erase(); flaky=T_SYSTEM+1; }
static void dead_blocks(void)
{
	struct airoha_bmt_progress done;
	unsigned a;
	/* where the next table would go */
	dead_table_slot(); assert(!reload());
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	assert(!storage(T_LAST-1)->spare[0][1] && stock_table()==T_LAST-2);
	assert(!reload()); assert_result(1);
	/* as the stage of a write, and of an erase's replacement */
	dead_stage(); assert(!reload() && tx.pool[1]==AIROHA_POOL_FREE);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	assert(!storage(T_SYSTEM+1)->spare[0][1] && tx.pool[1]==AIROHA_POOL_BAD);
	assert(!reload()); assert_result(1);
	dead_erase_stage(); assert(!reload());
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	assert(!storage(T_SYSTEM+1)->spare[0][1]);
	assert(!reload()); assert_result(1);
	/* the same, with the power cut as the replacement's record names it: the recovery gives
	 * the record up, as the write would have, and the erase asked again goes through */
	dead_erase_stage(); assert(!reload());
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	a=action_of('E',T_SYSTEM+1,0,0); assert(a);
	dead_erase_stage(); assert(!reload());
	cut_action=a; cut_phase=0; transaction_cut(); cut_action=0;
	assert(tx.has_pending && tx.pending.candidate_block==T_SYSTEM+1);
	assert(!reload() && !tx.has_pending && tx.pool[1]==AIROHA_POOL_BAD);
	assert(!reload());
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	/* a block that is nobody's, met by the recovery of a write that was cut */
	dead_bystander(); assert(!reload());
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	a=action_of('D',T_SYSTEM+1,5,0); assert(a);
	dead_bystander(); assert(!reload());
	cut_action=a; cut_phase=1; transaction_cut(); cut_action=0;
	assert(!reload() && tx.pool[5]==AIROHA_POOL_BAD && !storage(T_SYSTEM+5)->spare[0][1]);
	assert(!reload()); assert_result(0);
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	/* A replacement's first candidate fails its erase once and is marked; the power is cut
	 * before the record is cleared. The recovery does not erase the mark off and publish onto
	 * the block: it stays bad. */
	flaky_erase_stage(); assert(!airoha_bmt_transaction_apply(&tx,&change,&done));
	a=action_of('M',T_SYSTEM+1,0,0); assert(a);
	flaky_erase_stage();
	cut_action=a; cut_phase=1; transaction_cut(); cut_action=0;
	assert(!storage(T_SYSTEM+1)->spare[0][1]);
	assert(!reload() && !tx.has_pending);
	assert(!storage(T_SYSTEM+1)->spare[0][1] && tx.pool[1]==AIROHA_POOL_BAD);
	assert(airoha_bmt_translate(&t,64)!=(int)(T_SYSTEM+1));
	assert(!airoha_bmt_transaction_apply(&tx,&change,&done)); assert_result(1);
	puts("blocks that will not erase, as table slot, stage, replacement and bystander: marked and passed over: passed");
}

/* The old table's mark is finished, not taken as found. */
static void marks(void)
{
	void (*kinds[])(void)={remap_program,remap_erase};
	/* Cut short so that it reads as a mark now and as none later: the recovery marks it
	 * again, whole, and it stays the old table. (It used to be left: the day it read as no
	 * mark the old table was the table again, and the block was back on the original.) */
	for(unsigned k=0;k<2;k++) {
		unsigned mark=locate(kinds[k],'M',T_LAST,0,1);
		cut_at(kinds[k],mark,2);
		assert(storage(T_LAST)->spare[0][1]==0xfe);
		assert(!reload() && !tx.has_pending);
		assert(!storage(T_LAST)->spare[0][1]);
		fade();
		assert(stock_table() && stock_table()!=T_LAST); assert_result(1);
		assert(!reload()); assert_result(1);
	}
	/* Cut short so that it reads as none now and as a mark later, and at the next start the
	 * stage does not read. The original is whole (the write was into a used page), so the
	 * way back looks open; but a ticket is used, and with the new table cancelled the old
	 * one's mark would later leave no table at all. The start is refused, a table is found
	 * before and after the mark shows, and the next start finishes the write. */
	unsigned mark=locate(remap_used_page,'M',T_LAST,0,1);
	remap_used_page(); marginal_marks=1;
	cut_action=mark; cut_phase=2; transaction_cut(); cut_action=0; marginal_marks=0;
	assert(marginal[T_LAST] && stock_table()==T_LAST);
	unsigned stage=tx.pending.candidate_block;
	unreadable[stage][0]=200;
	assert(reload()); assert(stock_table()); assert_result(0);
	age();
	assert(stock_table() && stock_table()!=T_LAST);
	unreadable[stage][0]=0;
	assert(!reload() && !tx.has_pending); assert_result(1);
	assert(!storage(T_LAST)->spare[0][1]);
	puts("the old table's mark cut short, either way it can read: finished by the recovery, a table always found: passed");
}

/* A record whose erase was cut short reads as erased at one start and as itself at a later one.
 * By then the block it replaced has been written. It is not played again: the candidate is the
 * block's data, neither erased nor held against the record. */
static void replay(void)
{
	void (*kinds[])(void)={remap_erase,remap_program};
	for(unsigned k=0;k<2;k++) {
		unsigned clear=locate(kinds[k],'E',J,0,1);
		cut_at(kinds[k],clear,0); permanent_original_erase=0;
		memcpy(kept,storage(J)->main[0],T_PS);
		free(nand[J]); nand[J]=NULL;
		assert(!reload() && !tx.has_pending);
		int now=airoha_bmt_translate(&t,64); assert(now>=(int)T_SYSTEM);
		struct block *s=storage(now);
		for(unsigned p=T_PAGES-4;p<T_PAGES;p++) {	/* the system writes on, with no transaction */
			for(unsigned i=0;i<T_PS;i++) s->main[p][i]&=0xa5;
			s->spare[p][T_OS-1]=0; s->programs[p]++; s->last=p;
		}
		logical_image(now,after_image);
		memcpy(storage(J)->main[0],kept,T_PS); storage(J)->spare[0][T_OS-1]=0;
		assert(!reload() && !tx.has_pending && erased(NULL,J,0)==1);
		assert(airoha_bmt_translate(&t,64)==now);
		logical_image(now,image); assert(!memcmp(image,after_image,sizeof image));
		assert(!reload());
	}
	puts("a record read back after its work was done: not played again, the block's data kept: passed");
}

/* What is in the journal's block and is no record of ours is not all the same thing. */
static void foreign_records(void)
{
	unsigned mark=locate(remap_program,'M',T_LAST,0,1);
	unsigned char *j;
	/* a whole record in a format this build does not read: refused, and left */
	cut_at(remap_program,mark,0);
	j=storage(J)->main[0]; memcpy(kept,j,T_PS);
	j[7]='4'; j[8]=4; hash(j,208,j+208);
	assert(reload() && erased(NULL,J,0)==0 && j[7]=='4');
	memcpy(j,kept,T_PS);
	assert(!reload()); assert_result(1);
	/* a table where the journal should be: refused, and left */
	initial(0,0);
	assert(!airoha_bmt_encode(&t,expected,T_PS));
	memcpy(storage(J)->main[0],expected,T_PS); storage(J)->spare[0][T_OS-1]=0;
	assert(reload() && !memcmp(storage(J)->main[0],expected,T_PS));
	/* bytes that are neither: no record, erased, the writer starts */
	initial(0,0);
	memset(storage(J)->main[0],0x5a,T_PS); storage(J)->spare[0][T_OS-1]=0;
	assert(!reload() && erased(NULL,J,0)==1);
	puts("the journal's block holding another format's record, a table, or neither: passed");
}

int main(void)
{
	basic(); resets(); erase_resets();
	unstable_reads(); dead_blocks(); marks(); replay(); foreign_records();
	for(unsigned b=0;b<T_TOTAL;b++) free(nand[b]);
	return 0;
}
