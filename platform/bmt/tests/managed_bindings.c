/* SPDX-License-Identifier: GPL-2.0-only */
/* Execute each shipped mutable binding against a stateful physical NAND. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int fail_alloc;
#include LAYER_FILE
#include "geometry.h"
#ifdef TEST_LINUX
#define attach sdr_bmt_attach
#define detach sdr_bmt_detach
#else
#define attach airoha_bmt_attach
#define detach airoha_bmt_detach
#endif
#define TRANSACTION_MODEL
#include "publication.c"

#define BS T_BS
#define PS T_PS
static struct mtd_info m;
static unsigned char data[2*BS], oob[2*T_PAGES*T_OS], expected_data[2*BS], expected_oob[2*T_PAGES*T_OS];
static unsigned mutations, physical_reads;
static int fail_original, fail_erase_original, fail_metadata, fail_read_ecc, fail_marker, timeout_next;
static int reenter, reentered;
static unsigned original_physical;
#ifdef TEST_LINUX
static pthread_mutex_t pause_lock=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t pause_cv=PTHREAD_COND_INITIALIZER;
static int pause_original, paused, release_original, reader_waiting, reader_done;
static _Thread_local int reader_role;
#endif

static unsigned auto_column(unsigned c) { return (c/14)*16+2+c%14; }
static unsigned physical(unsigned logical)
{ unsigned b=logical+(logical>=65 && logical<(T_SYSTEM-1)); return b==67 ? (T_SYSTEM+4) : b; }
static unsigned char pattern(unsigned b,unsigned p,unsigned c) { return (b*3+p*13+c*7)%251; }

static int driver_read(struct mtd_info *dev,loff_t from,struct mtd_oob_ops *ops)
{
	unsigned per=ops->mode==MTD_OPS_AUTO_OOB ? T_FREE : T_OS;
	unsigned b=(u64)from/BS;
	(void)dev; physical_reads++;
	ops->retlen=ops->oobretlen=0;
	if(timeout_next) { timeout_next=0; return -ETIMEDOUT; }
	if(fail_marker && !ops->len && ops->mode==MTD_OPS_RAW && ops->ooblen==2) return -EIO;
	for(size_t i=0;i<ops->len;i++) {
		u64 a=(u64)from+i; struct block *s=nand[a/BS];
		ops->datbuf[i]=s ? s->main[(a%BS)/PS][a%PS] : 255;
	}
	for(size_t i=0;i<ops->ooblen;i++) {
		u64 page=(u64)from/PS+(i+ops->ooboffs)/per;
		unsigned col=(i+ops->ooboffs)%per;
		if(ops->mode==MTD_OPS_AUTO_OOB) col=auto_column(col);
		struct block *s=nand[page/T_PAGES];
		ops->oobbuf[i]=s ? s->spare[page%T_PAGES][col] : 255;
	}
	ops->retlen=ops->len;ops->oobretlen=ops->ooblen;
	if(fail_read_ecc && b==original_physical && ops->mode!=MTD_OPS_RAW) return -EBADMSG;
	return 0;
}
static int driver_write(struct mtd_info *dev,loff_t to,struct mtd_oob_ops *ops)
{
	unsigned b=(u64)to/BS,p=((u64)to%BS)/PS,per=ops->mode==MTD_OPS_AUTO_OOB ? T_FREE : T_OS;
	struct block *s=storage(b);
	unsigned char main_page[T_PS], spare[T_OS];
	(void)dev;
	assert(b>=T_FIRST && b!=65 && b!=T_SYSTEM); /* physical protected/factory storage */
	assert(!((u64)to%PS) && ops->len<=PS && ops->ooboffs+ops->ooblen<=per);
	ops->retlen=ops->oobretlen=0; mutations++;
	/* the publication's last step fails: the old table takes no bad mark (a block that will
	 * not erase no longer stops one: it is marked and passed over) */
	if(fail_metadata && b==T_LAST && ops->mode==MTD_OPS_RAW && !ops->len) return -EIO;
#ifndef TEST_LINUX
	if(reenter && !reentered) {
		struct mtd_oob_ops recursive={.mode=MTD_OPS_AUTO_OOB,.len=1,.datbuf=data};
		reentered=1; assert(m._write_oob(&m,64ull*BS,&recursive)==-EBUSY);
	}
#endif
#ifdef TEST_LINUX
	if(pause_original && b==original_physical) {
		pthread_mutex_lock(&pause_lock); paused=1; pthread_cond_broadcast(&pause_cv);
		while(!release_original) pthread_cond_wait(&pause_cv,&pause_lock);
		pthread_mutex_unlock(&pause_lock);
	}
#endif
	assert((int)p>=s->last && s->programs[p]<4);
	s->last=p;s->programs[p]++;
	memset(main_page,255,sizeof main_page);memset(spare,255,sizeof spare);
	if(ops->len) memcpy(main_page,ops->datbuf,ops->len);
	for(size_t i=0;i<ops->ooblen;i++) {
		unsigned col=ops->ooboffs+i;
		if(ops->mode==MTD_OPS_AUTO_OOB) col=auto_column(col);
		spare[col]=ops->oobbuf[i];
	}
	int error=(fail_original && b==original_physical && ops->len);
	if(error) fail_original=0;
	unsigned bytes=error ? (T_PS/2) : T_PS;
	for(unsigned i=0;i<bytes;i++) {
		if(i<ops->len) assert((s->main[p][i]&main_page[i])==main_page[i]);
		s->main[p][i]&=main_page[i];
	}
	for(unsigned i=0;i<T_OS;i++) s->spare[p][i]&=spare[i];
	if(ops->mode!=MTD_OPS_RAW) s->spare[p][T_OS-1]=0;
	ops->retlen=ops->len<bytes ? ops->len : bytes;
	ops->oobretlen=ops->ooblen;
	return error ? -EIO : 0;
}
static int driver_erase(struct mtd_info *dev,struct erase_info *e)
{
	unsigned b=e->addr/BS; (void)dev;
	assert(e->addr%BS==0 && e->len==BS && b>=64 && b!=65 && b!=T_SYSTEM && b!=T_LAST);
	mutations++;
	if(fail_erase_original && b==original_physical) { fail_erase_original=0;return -EIO; }
	struct block *s=storage(b);memset(s,255,sizeof *s);memset(s->programs,0,T_PAGES);s->last=-1;
	return 0;
}
static int driver_isbad(struct mtd_info *dev,loff_t from)
{ (void)dev; struct block *s=nand[(u64)from/BS];return s && (s->spare[0][0]!=255 || s->spare[0][1]!=255); }
static int driver_markbad(struct mtd_info *dev,loff_t from)
{ (void)dev;(void)from;assert(!"generic markbad must not be called");return -EIO; }
static void raw_mtd(void)
{
	memset(&m,0,sizeof m);m.size=0x20000000ULL;m.erasesize=BS;m.writesize=PS;
	m.oobsize=T_OS;m.oobavail=T_FREE;m.flags=MTD_WRITEABLE;
	m._read_oob=driver_read;m._write_oob=driver_write;m._erase=driver_erase;
	m._block_isbad=driver_isbad;m._block_markbad=driver_markbad;
}
static void base(int populated)
{
	detach(&m);
	for(unsigned b=0;b<T_TOTAL;b++) { free(nand[b]);nand[b]=NULL; }
	raw_mtd();fail_alloc=fail_original=fail_erase_original=fail_metadata=fail_read_ecc=fail_marker=timeout_next=0;
	reenter=reentered=0;bmt_transport_failed=0;
	original_physical=64;
	assert(!airoha_bmt_set_geometry(&t,T_TOTAL,T_POOL) && !airoha_bmt_set_pages(&t,T_PS,T_FREE));t.bbt_loaded=t.bmt_loaded=t.bbt_ascending=1;
	t.bbt_size=1;t.bbt_entries[0]=65;t.bmt_size=1;t.bmt_from[0]=67;t.bmt_to[0]=(T_SYSTEM+4);
	original=t;
	assert(!airoha_bbt_encode(&t,storage(T_SYSTEM)->main[0],T_PS));storage(T_SYSTEM)->programs[0]=1;storage(T_SYSTEM)->last=0;
	assert(!airoha_bmt_encode(&t,storage(T_LAST)->main[0],T_PS));storage(T_LAST)->programs[0]=1;storage(T_LAST)->last=0;
	storage(65)->spare[0][0]=0;
	/* The LV's pool comes from the vendor scan: two factory-bad blocks in its
	 * span make it 165, which the tables below assume. */
	if(T_FACTORY_TOP(T_FACTORY_TOP_A)) {
		storage(T_FACTORY_TOP_A)->spare[0][0]=0; storage(T_FACTORY_TOP_B)->spare[0][0]=0;
	}
	if(populated) for(unsigned logical=64;logical<69;logical++) {
		unsigned b=physical(logical);struct block *s=storage(b);
		for(unsigned p=0;p<T_PAGES;p++) {
			for(unsigned i=0;i<T_PS;i++) s->main[p][i]=pattern(b,p,i);
			for(unsigned i=0;i<T_OS;i++) s->spare[p][i]=pattern(b,p,T_PS+i);
			s->spare[p][0]=s->spare[p][1]=255;
			s->spare[p][2]=b>=T_SYSTEM ? 67 : 255;s->spare[p][3]=b>=T_SYSTEM ? 0 : 255;
			s->programs[p]=1;s->last=p;
		}
	}
	mutations=physical_reads=0;
	assert(!attach(&m));assert(!mutations && layer.writer && layer.writer->ready);
}

static void read_matrix(void)
{
	unsigned cases=0;
	base(1);
	for(unsigned mode=0;mode<3;mode++) for(unsigned logical=64;logical<=66;logical++)
	for(unsigned start=0;start<3;start++) for(unsigned kind=0;kind<5;kind++) {
		unsigned per=mode==MTD_OPS_AUTO_OOB ? T_FREE : T_OS, off=11;
		u64 addr=(u64)logical*BS+BS-PS*(start+1)+17;
		size_t len=kind==0 ? 0 : (kind==3 ? 23 : BS+PS*2);
		size_t olen=kind==1 ? 0 : (kind==4 ? 9 : per*(T_PAGES+4)-11);
		for(size_t i=0;i<len;i++) { u64 a=addr+i;unsigned b=physical(a/BS);expected_data[i]=storage(b)->main[(a%BS)/PS][a%PS]; }
		for(size_t i=0;i<olen;i++) { u64 page=addr/PS+(i+off)/per;unsigned col=(i+off)%per;
			if(mode==MTD_OPS_AUTO_OOB) col=auto_column(col);
			expected_oob[i]=storage(physical(page/T_PAGES))->spare[page%T_PAGES][col]; }
		struct mtd_oob_ops op={.mode=mode,.len=len,.ooblen=olen,.ooboffs=off,
			.datbuf=len ? data : NULL,.oobbuf=olen ? oob : NULL};
		assert(!m._read_oob(&m,addr,&op) && op.retlen==len && op.oobretlen==olen);
		assert(!memcmp(data,expected_data,len) && !memcmp(oob,expected_oob,olen));cases++;
	}
	printf("managed translated read/OOB boundary cases: %u\n",cases);
}
static void writes(void)
{
	struct mtd_oob_ops op;
	/* An erase immediately followed by a first-page write takes the direct
	 * path and performs exactly one programmed-page readback. */
	base(0);memset(data,0x5c,PS);
	unsigned erase_mutations=mutations,erase_reads=physical_reads;
	struct erase_info fast_erase={.addr=64ull*BS,.len=BS};
	assert(!m._erase(&m,&fast_erase));
	struct erase_info other_erase={.addr=66ull*BS,.len=BS};
	assert(!m._erase(&m,&other_erase));
	assert(mutations==erase_mutations+2 && physical_reads==erase_reads);
	assert(layer.counters.erase_attempts==2 && layer.counters.erase_completed==2);
	unsigned before_mutations=mutations,before_reads=physical_reads;
	unsigned before_programs=layer.counters.program_completed;
	u64 before_readbacks=layer.counters.readback_completed;
	/* UBI's format erase immediately following the admitted bulk erase is
	 * redundant in this boot and must not touch NAND a second time. */
	fast_erase=(struct erase_info){.addr=64ull*BS,.len=BS};
	assert(!m._erase(&m,&fast_erase) && mutations==before_mutations);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS,&op) && op.retlen==PS);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,66ull*BS,&op) && op.retlen==PS);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS+PS,&op) && op.retlen==PS);
	assert(mutations==before_mutations+3 && physical_reads==before_reads+3);
	assert(layer.counters.program_attempts==before_programs+3 &&
	       layer.counters.program_completed==before_programs+3 &&
	       layer.counters.readback_completed==before_readbacks+3 &&
	       !layer.counters.markbad_attempts && !layer.counters.markbad_completed);
	assert(!layer.writer->remaps && storage(64)->main[0][0]==0x5c &&
	       storage(64)->main[1][0]==0x5c && storage(T_SYSTEM+4)->main[0][0]==0x5c);
	/* The same erase-then-write rule covers `durable`, opened on demand, without WAL
	 * (its first block: 0x2a00000 on both parts). */
	struct erase_info scratch_erase={.addr=(0x2a00000ull),.len=BS};
	assert(!m._erase(&m,&scratch_erase));before_mutations=mutations;before_reads=physical_reads;
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,(0x2a00000ull),&op) && op.retlen==PS);
	assert(mutations==before_mutations+1 && physical_reads==before_reads+1);
	/* A block erased in an earlier boot (a UBI free PEB: an EC header in page
	 * 0, the rest erased) is learned once, then appended to directly: one
	 * program and one readback per page, no erase, no journal, no remap. */
	base(0);
	{
		struct block *s=storage(physical(68));
		memset(s->main[0],0x11,PS); s->programs[0]=1; s->last=0;
	}
	before_mutations=mutations;
	u64 before_erases=layer.counters.erase_attempts;
	before_readbacks=layer.counters.readback_attempts;
	memset(data,0x6e,PS);
	for(unsigned page=1;page<=3;page++) {
		op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
		assert(!m._write_oob(&m,68ull*BS+page*PS,&op) && op.retlen==PS);
	}
	assert(mutations==before_mutations+3 && layer.counters.erase_attempts==before_erases);
	assert(layer.counters.readback_attempts==before_readbacks+T_PAGES+3);
	assert(!layer.writer->remaps && storage(physical(68))->programs[0]==1);
	for(unsigned page=1;page<=3;page++) assert(storage(physical(68))->main[page][7]==0x6e);
	/* A page below the highest in use is not appended to: it is remapped. */
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,68ull*BS,&op) && op.retlen==PS && layer.writer->remaps==1);
	/* A failed direct program falls back to the durable remap path. */
	base(0);fast_erase=(struct erase_info){.addr=64ull*BS,.len=BS};
	assert(!m._erase(&m,&fast_erase));fail_original=1;
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS,&op) && op.retlen==PS);
	assert(layer.writer->remaps==1);

	base(0);memset(data,0xa7,sizeof data);reenter=1;
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS,&op) && op.retlen==PS);
#ifndef TEST_LINUX
	assert(reentered);
#endif
	memset(oob,0x61,sizeof oob);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_PLACE_OOB,.len=73,.datbuf=data,.ooblen=12,.ooboffs=4,.oobbuf=oob};
	assert(!m._write_oob(&m,64ull*BS+PS+17,&op) && op.retlen==73 && op.oobretlen==12);
	fail_original=1;
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS+2*PS,&op) && op.retlen==PS);
	assert(layer.writer->remaps==1 && airoha_bmt_translate(&layer.t,64)>=(int)T_SYSTEM);
	unsigned former=original_physical=airoha_bmt_translate(&layer.t,64);
	fail_original=1;
	assert(!m._write_oob(&m,64ull*BS+3*PS,&op) && op.retlen==PS);
	assert(layer.writer->remaps==2 && airoha_bmt_translate(&layer.t,64)!=(int)former);
	int quarantined=0;
	for(unsigned i=0;i<layer.t.bmt_size;i++)
		if(layer.t.bmt_from[i]==former && layer.t.bmt_to[i]==former) quarantined=1;
	assert(quarantined);
	unsigned selected=stock_table();assert(selected==layer.writer->bmt_block);
	detach(&m);raw_mtd();assert(!attach(&m));
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	memset(data,0,sizeof data);assert(!m._read_oob(&m,64ull*BS+2*PS,&op));
	for(unsigned i=0;i<PS;i++) assert(data[i]==0xa7);
	/* Two logical blocks across factory shifting; no double shift. */
	base(0);memset(data,0x39,sizeof data);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=PS+31,.datbuf=data};
	assert(!m._write_oob(&m,64ull*BS+BS-17,&op) && op.retlen==PS+31);
	assert(storage(64)->main[63][T_PS-17]==0x39 && storage(66)->main[0][0]==0x39);
	base(0);fail_erase_original=1;
	struct erase_info erase={.addr=64ull*BS,.len=BS};
	assert(!m._erase(&m,&erase) && layer.writer->remaps==1);
	op=(struct mtd_oob_ops){.mode=MTD_OPS_AUTO_OOB,.len=BS,.datbuf=data};
	assert(!m._read_oob(&m,64ull*BS,&op));
	for(unsigned i=0;i<BS;i++) assert(data[i]==255);
	puts("managed writes: full/partial main, PLACE OOB, repeated remap/quarantine, erase remap, reattach and BBT crossing passed");
}
static void refusals(void)
{
	base(0);unsigned prior=mutations;
	for(unsigned b=0;b<T_TOTAL;b++) if(b<T_FIRST || b>=T_END) {
		struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=1,.datbuf=data};
		struct erase_info e={.addr=(u64)b*BS,.len=BS};
		assert(m._write_oob(&m,(u64)b*BS,&op)==-EROFS && !op.retlen);
		assert(m._erase(&m,&e)==-EROFS);assert(m._block_markbad(&m,(u64)b*BS)==-EROFS);
	}
	assert(prior==mutations);
	for(unsigned mode=0;mode<3;mode++) {
		struct mtd_oob_ops op={.mode=mode,.len=1,.datbuf=data,.ooblen=1,.oobbuf=oob};
		assert(m._write_oob(&m,64ull*BS,&op)==-EOPNOTSUPP && !op.retlen && !op.oobretlen);
	}
	assert(m._block_markbad(&m,64ull*BS)==-EOPNOTSUPP && prior==mutations);
	struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=2,.datbuf=data};
	assert(m._write_oob(&m,T_END*BS-1,&op)==-EROFS && !op.retlen);
	fail_read_ecc=1;op.len=PS;
	assert(m._read_oob(&m,64ull*BS,&op)==-EBADMSG && op.retlen==PS);
	assert(m._write_oob(&m,64ull*BS,&op)==-EBADMSG && !op.retlen && prior==mutations);
	fail_read_ecc=0;
	detach(&m);raw_mtd();fail_alloc=1;assert(attach(&m)==-ENOMEM && !layer.mtd);fail_alloc=0;
	fail_marker=1;assert(attach(&m)==-EIO && !layer.mtd);fail_marker=0;
	assert(!attach(&m));
	puts("managed master protection, RAW/marker/parity refusal, ECC counts, markbad and ENOMEM: passed");
}
/* A writer that cannot start (here: the journal's block is marked bad) used to fail the attach,
 * and then nothing read the flash at all. It attaches with translation alone: what was there
 * reads as it did, through the same map, and nothing is written. */
static void writer_that_cannot_start(void)
{
	struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	base(1);
	unsigned journal=layer.writer->journal_block;
	for(unsigned logical=64;logical<69;logical++) {	/* 67 is the one the BMT maps into the pool */
		op.retlen=0;assert(!m._read_oob(&m,(loff_t)logical*BS,&op) && op.retlen==PS);
		memcpy(expected_data+(logical-64)*PS,data,PS);
	}
	detach(&m);raw_mtd();
	storage(journal)->spare[0][0]=0;
	unsigned before=mutations;
	assert(!attach(&m));
	assert(layer.mtd==&m && layer.read_only && !layer.writer);
	/* Not writeable, and still a device the MTD core registers: add_mtd_device refuses one
	 * with no erase operation (drivers/mtd/mtdcore.c: "!mtd->erasesize || !master->_erase",
	 * unless MTD_NO_ERASE). A first version cleared the operations, and the probe then
	 * failed at registration: the very thing this attachment is for. They answer -EROFS. */
	assert(!(m.flags&MTD_WRITEABLE) && m.erasesize && m._erase && m._write_oob && m._block_markbad);
	struct erase_info refused={.addr=64ull*BS,.len=BS};
	op.retlen=0;
	assert(m._erase(&m,&refused)==-EROFS && m._write_oob(&m,64ull*BS,&op)==-EROFS && !op.retlen);
	assert(m._block_markbad(&m,64ull*BS)==-EROFS);
	for(unsigned logical=64;logical<69;logical++) {
		memset(data,0,PS);op.retlen=0;
		assert(!m._read_oob(&m,(loff_t)logical*BS,&op) && op.retlen==PS);
		assert(!memcmp(data,expected_data+(logical-64)*PS,PS));
	}
	assert(mutations==before);
	detach(&m);assert(m.flags&MTD_WRITEABLE);
	puts("a writer that cannot start: attached read-only, the same translated reads, nothing written: passed");
}

static void recovery(void)
{
	/* The direct program tears its page; the transaction then stages the
	 * intended block and remaps, and here the old table's mark fails too. The
	 * caller is told nothing was made durable; recovery publishes it. */
	base(0);memset(data,0x57,sizeof data);fail_original=fail_metadata=1;
	struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	assert(m._write_oob(&m,64ull*BS,&op)==-EIO && !op.retlen && layer.writer->failed);
	assert(layer.writer->has_pending);
	unsigned prior=mutations;
	assert(m._write_oob(&m,64ull*BS,&op)==-EIO && !op.retlen && mutations==prior);
	assert(m._read_oob(&m,64ull*BS,&op)==-EIO && !op.retlen);
	detach(&m);raw_mtd();fail_metadata=0;
	assert(!attach(&m) && layer.writer->recoveries==1 && layer.writer->ready);
	memset(data,0,PS);assert(!m._read_oob(&m,64ull*BS,&op));
	for(unsigned i=0;i<PS;i++) assert(data[i]==0x57);
	base(0);timeout_next=1;op.len=PS;
	assert(m._read_oob(&m,64ull*BS,&op)==-ETIMEDOUT);
	prior=physical_reads;assert(m._read_oob(&m,64ull*BS,&op)==-EIO && physical_reads==prior);
	detach(&m);raw_mtd();assert(attach(&m)==-EIO);bmt_transport_failed=0;
	puts("managed failed-transaction guard, recovery-before-attach and transport reset latch: passed");
}

#ifdef TEST_LINUX
static void wait_at_lock(pthread_mutex_t *lock)
{
	if(!reader_role || lock!=&bmt_lock) return;
	pthread_mutex_lock(&pause_lock);reader_waiting=1;pthread_cond_broadcast(&pause_cv);
	pthread_mutex_unlock(&pause_lock);
}
static void *write_thread(void *unused)
{
	struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=data};
	(void)unused;assert(!m._write_oob(&m,64ull*BS,&op) && op.retlen==PS);return NULL;
}
static void *read_thread(void *unused)
{
	unsigned char result[PS];
	struct mtd_oob_ops op={.mode=MTD_OPS_AUTO_OOB,.len=PS,.datbuf=result};
	(void)unused;reader_role=1;
	assert(!m._read_oob(&m,64ull*BS,&op) && op.retlen==PS);
	for(unsigned i=0;i<PS;i++) assert(result[i]==0x37);
	pthread_mutex_lock(&pause_lock);reader_done=1;pthread_mutex_unlock(&pause_lock);return NULL;
}
static void concurrent(void)
{
	pthread_t writer,reader;
	base(0);memset(data,0x37,PS);pause_original=1;test_before_mutex_lock=wait_at_lock;
	assert(!pthread_create(&writer,NULL,write_thread,NULL));
	pthread_mutex_lock(&pause_lock);
	while(!paused) pthread_cond_wait(&pause_cv,&pause_lock);
	pthread_mutex_unlock(&pause_lock);
	assert(!pthread_create(&reader,NULL,read_thread,NULL));
	pthread_mutex_lock(&pause_lock);
	while(!reader_waiting) pthread_cond_wait(&pause_cv,&pause_lock);
	assert(!reader_done && pthread_mutex_trylock(&bmt_lock)==EBUSY);
	release_original=1;pthread_cond_broadcast(&pause_cv);pthread_mutex_unlock(&pause_lock);
	assert(!pthread_join(writer,NULL) && !pthread_join(reader,NULL));
	assert(reader_done);pause_original=0;test_before_mutex_lock=NULL;
	puts("actual Linux binding: concurrent reader waits for complete mutation and sees committed bytes");
}
#endif

#ifdef UBI_CONSUMER_FILE
#include UBI_CONSUMER_FILE
#endif

int main(void)
{
	read_matrix();writes();refusals();recovery();writer_that_cannot_start();
#ifdef TEST_LINUX
	concurrent();
#endif
#ifdef UBI_CONSUMER_FILE
	ubi_consumers();
#endif
	detach(&m);
	for(unsigned b=0;b<T_TOTAL;b++) free(nand[b]);
	return 0;
}
