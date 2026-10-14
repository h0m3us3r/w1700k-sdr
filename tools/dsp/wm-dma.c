/* SPDX-License-Identifier: MIT */
/* MEM_DMA TX0 -> RX0, one outstanding descriptor, bounded polling.
 * No payload loads/stores on the WM. Caller owns idle MEM_DMA and ULM1.
 * Uses existing ROM prefetch allocation; leaves queues stopped on exit.
 * Results at 0x00910a00. Return 0 on verified descriptor completions,
 * 1 bad arguments, 2 busy, 3 timeout, 4 RX descriptor error, 5 stop timeout.
 * Content/guard validation belongs to the caller, after completion.
 */
#include <stdint.h>
#define REG(o) (*(volatile uint32_t *)(0x58000000u+(o)))
#define TX ((volatile uint32_t *)0x00910800)
#define RX ((volatile uint32_t *)0x00910900)
struct result { uint32_t completed, sum, minimum, maximum, total, tx, rx, irq, error; };
#define OUT ((volatile struct result *)0x00910a00)
#define TIMEOUT 7200000u
static inline uint32_t cycles(void) { uint32_t v; __asm__ volatile("rdcycle %0":"=r"(v)); return v; }
static inline void fence(void) { __asm__ volatile("fence iorw, iorw":::"memory"); }

uint32_t dma_trial(uint32_t source, uint32_t dest, uint32_t bytes, uint32_t reps)
{
    uint32_t cfg, begin, t, elapsed, result=0;
    OUT->completed=OUT->sum=OUT->maximum=OUT->total=OUT->error=0;
    OUT->minimum=0xffffffffu;
    /* Explicit endpoint extents: scratch source, or either stopped ICAP
     * path-0 page. DTCM destination stays inside the raw-IQ reservation. */
    if (!(bytes>=16 && bytes<=8192 && !(bytes&15) && reps && reps<=4096 &&
          (source==0x00912000 || (source==0x00464000 && bytes<=4096) ||
           (source==0x00466000 && bytes<=4096) || source==0xe0400000) &&
          dest>=0x8620d000 && dest<=0x8620f000-bytes && !(dest&15))) return 1;
    cfg=REG(0x208);
    if (cfg&10 || REG(0x308)!=REG(0x30c)) return 2;
    REG(0x208)=cfg&~5u;
    for (uint32_t i=0;i<8;i++) {
        TX[4*i]=source; TX[4*i+1]=0x80000000; TX[4*i+2]=TX[4*i+3]=0;
        RX[4*i]=dest; RX[4*i+1]=bytes<<16; RX[4*i+2]=RX[4*i+3]=0;
    }
    fence();
    REG(0x300)=0x00910800; REG(0x304)=8; REG(0x308)=0;
    REG(0x500)=0x00910900; REG(0x504)=8; REG(0x508)=0;
    REG(0x20c)=1; REG(0x280)=1;
    REG(0x508)=7;
    REG(0x208)=(cfg&~10u)|5u;
    begin=cycles();
    for (uint32_t i=0;i<reps;i++) {
        uint32_t slot=i&7, next=(slot+1)&7;
        TX[slot*4+1]=0x40000000|(bytes<<16);
        fence();
        t=cycles();
        REG(0x308)=next;
        /* DIDX may advance before the RX descriptor's completion write is
         * visible. Wait for both; an I/O fence alone cannot finish DMA. */
        while (REG(0x30c)!=next || REG(0x50c)!=next || !(RX[slot*4+1]&0x80000000)) {
            if ((uint32_t)(cycles()-t)>TIMEOUT) { result=3; goto done; }
        }
        fence();
        elapsed=cycles()-t;
        uint32_t ctrl=RX[slot*4+1];
        if (!(ctrl&0x80000000) || ((ctrl>>16)&0x3fff)!=bytes) { result=4; goto done; }
        OUT->completed++;
        OUT->sum+=elapsed;
        if (elapsed<OUT->minimum) OUT->minimum=elapsed;
        if (elapsed>OUT->maximum) OUT->maximum=elapsed;
        RX[slot*4+1]=bytes<<16;
        fence();
        REG(0x508)=slot;
    }
done:
    OUT->total=cycles()-begin;
    OUT->tx=REG(0x30c); OUT->rx=REG(0x50c); OUT->irq=REG(0x110);
    REG(0x208)=cfg&~5u;
    t=cycles();
    while (REG(0x208)&10) {
        if ((uint32_t)(cycles()-t)>TIMEOUT) { result=5; break; }
    }
    /* Validation-only CPU reads follow the call. The ICAP pool is cached
     * by WM, so discard these clean source lines after the DMA measurement.
     * The transfer itself does not read or copy payload through the CPU. */
    if (!result && source==0xe0400000) {
        for (uint32_t a=source; a<source+bytes; a+=64) {
            __asm__ volatile("csrw 0x7cb, %0\n csrw 0x7cc, zero"::"r"(a):"memory");
        }
        fence();
    }
    OUT->error=result;
    return result;
}
