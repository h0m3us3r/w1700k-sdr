/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * w1700k_sdr: character-device ABI of the MT7996 SDR driver (/dev/w1700k-sdr).
 *
 * All fields are native-endian with a fixed layout on 32- and 64-bit hosts.
 * Every ioctl except GET_INFO requires CAP_SYS_RAWIO. Reserved fields must be
 * zero. Clients must check abi_version for exact equality: any layout change
 * bumps it.
 */
#ifndef W1700K_SDR_UAPI_H
#define W1700K_SDR_UAPI_H
#include <linux/ioctl.h>
#include <linux/types.h>

#define W1700K_SDR_ABI_VERSION 5

#define W1700K_SDR_CAP_HOST_OWN (1U << 0)
#define W1700K_SDR_CAP_DOWNLOAD (1U << 1)
#define W1700K_SDR_CAP_RESET (1U << 2)
#define W1700K_SDR_CAP_MEM (1U << 3)
/* Bits 4 and 5 (BRINGUP_WRITE, MCU_CMD) were retired with ABI 5. */
#define W1700K_SDR_CAP_STREAM_RX (1U << 6)
#define W1700K_SDR_CAP_CTL (1U << 7)
#define W1700K_SDR_CAP_LED (1U << 8)

/* Static identity, served without touching the device. */
struct w1700k_sdr_info {
	__u32 abi_version;
	__u32 capabilities;	/* W1700K_SDR_CAP_* */
	__u16 vendor, device;	/* PCI identity */
	__u8 revision;
	__u8 reserved0[3];
	__u32 max_read;		/* READ_MEM length limit, bytes */
	__u32 reserved1;
	__aligned_u64 bar0_size;
};
#define W1700K_SDR_GET_INFO _IOR('Q', 0x00, struct w1700k_sdr_info)

/*
 * Take band-0 host ownership of the MCU interface (LPCR DRV_OWN, then poll
 * FW_OWN_STAT clear, then acknowledge). Argument must be 0. Every operation
 * that touches the device requires ownership; RESET_MT returns it to the
 * firmware, so acquire again after a reset.
 */
#define W1700K_SDR_ACQUIRE_HOST _IO('Q', 0x11)

/*
 * Download one firmware container. The format is detected: a RAM container
 * (WM, WA or DSP; trailer and CRC32 at the end) or the ROM patch (big-endian
 * header at the front). A full bring-up is the patch, then WM, DSP and WA in
 * one session; each RAM type is started once. Any failure latches the session
 * until RESET_MT.
 *
 * The first download enables bus mastering, allocates and binds the DMA rings
 * (including the 1 GiB stream ring) and pins the module. Only RESET_MT undoes
 * that.
 *
 * Returns 0, a positive raw device status byte, or a negative errno. The
 * output fields describe how far the schedule got (`phase`, below).
 * sem_get_status/sem_release_status are the raw PATCH_SEM_CONTROL replies
 * (-1 when not sent): get 1 = patch already present (regions skipped), 2 =
 * granted; release 3 = released.
 */
#define W1700K_SDR_PHASE_PREFLIGHT 0U
#define W1700K_SDR_PHASE_DISCARD_EVENTS 1U
#define W1700K_SDR_PHASE_INIT 2U
#define W1700K_SDR_PHASE_SCATTER 3U
#define W1700K_SDR_PHASE_START 4U
#define W1700K_SDR_PHASE_COMPLETE 5U
#define W1700K_SDR_PHASE_WAIT_DOWNLOAD 6U
#define W1700K_SDR_PHASE_PATCH_SEM_GET 7U
#define W1700K_SDR_PHASE_PATCH_FINISH 8U
#define W1700K_SDR_PHASE_PATCH_SEM_RELEASE 9U

#define W1700K_SDR_RAM_WM 0U
#define W1700K_SDR_RAM_WA 1U
#define W1700K_SDR_RAM_DSP 2U

#define W1700K_SDR_CONTAINER_RAM 1U
#define W1700K_SDR_CONTAINER_PATCH 2U

struct w1700k_sdr_download {
	__aligned_u64 image;	/* in: user pointer to the whole file */
	__u32 bytes;		/* in */
	__u32 ram_type;		/* in: W1700K_SDR_RAM_*; ignored for the patch */
	__u32 container;	/* out: W1700K_SDR_CONTAINER_* */
	__u32 phase;		/* out */
	__u32 region, offset;	/* out: region index and byte offset reached */
	__u32 tx_submitted, tx_completed, replies;	/* out */
	__u32 last_fw_state;	/* out: last FW_STATE word read (BAR 0xe00f0) */
	__s32 sem_get_status, sem_release_status;	/* out */
	__s32 result;		/* out: same as the return value */
	__u32 reserved;
};
#define W1700K_SDR_DOWNLOAD _IOWR('Q', 0x15, struct w1700k_sdr_download)

/*
 * WiFi subsystem reset (MT_WF_SUBSYS_RST 0x70028600 bit 0 pulsed, 20 ms each
 * edge, as mt76 does at probe). Stops WFDMA, resets, clears bus mastering,
 * frees every ring and unpins the module. The MCU is back in its ROM download
 * state and owns band 0 again. Refused with -EBUSY while the stream ring is
 * mapped or exported datagrams are still queued on a network device.
 */
struct w1700k_sdr_reset {
	__s32 stop_observe;	/* WFDMA stop result; -EALREADY if never enabled */
	__u32 fw_state_after;	/* BAR 0xe00f0 after the reset */
	__u32 owner_after;	/* BAR 0xe0010 (LPCR) after the reset */
	__u32 rings_released;
	__u32 was_dma_exposed;
	__u32 reserved[3];
};
#define W1700K_SDR_RESET_MT _IOR('Q', 0x16, struct w1700k_sdr_reset)

/*
 * Read device memory through the mt76 BAR translation (direct, fixed map, L1
 * or L2 window), for diagnostics. Word aligned, at most max_read bytes, and
 * the whole span must lie inside one permitted window:
 *   0x000e0000-0x000e0fff  host CSR page (LPCR 0xe0010, FW_STATE 0xe00f0)
 *   0x00800000-0x0080ffff  MCU ROM
 *   0x00900000-0x0090ffff  patch SRAM (ROM patch, our WM's mailbox page)
 * Other addresses return -EPERM: host reads inside the WiFi power island can
 * hang the SoC and wedge the MT7996 until power is removed.
 */
struct w1700k_sdr_mem_read {
	__u32 address, length;
	__aligned_u64 data;	/* user buffer, `length` bytes */
	__u32 reserved[2];
};
#define W1700K_SDR_READ_MEM _IOW('Q', 0x17, struct w1700k_sdr_mem_read)

/*
 * Streaming. The WM event ring (WFDMA0 RX ring 0, fed by the MCU's own TX
 * ring 0) has 1536 descriptors over one circular buffer of `slices` slices of
 * `slice_bytes` (131072 x 8 KiB = 1 GiB): completion c lands in slice
 * c % slices. mmap the device PROT_READ, MAP_SHARED, offset 0, length
 * header_bytes + slices * slice_bytes to get a struct w1700k_sdr_stream_ring
 * followed by the slices. Slices below `completed` (load-acquire) are valid
 * until the ring laps them: a reader more than slices - slots behind has lost
 * data. RESET_MT refuses while a mapping exists.
 */
struct w1700k_sdr_stream_ring {
	__u64 completed;	/* completions so far */
	__u32 slots;		/* RX descriptors */
	__u32 slices;
	__u32 slice_bytes;
	__u32 header_bytes;	/* offset of slice 0 in the mapping */
	__u32 reserved[10];
	__u32 ctrl[];		/* per slice: descriptor ctrl word; length in [29:16] */
};

/*
 * Optional export: every completed slice leaves as one UDP datagram straight
 * from the ring pages (no copy; the MAC computes the UDP checksum): Ethernet,
 * IPv4 (DF), UDP, struct w1700k_sdr_export_header, then the slice's bytes.
 * The network device's queue is the only flow control; a slice the device
 * could already be rewriting ends the export with -ENOBUFS.
 *
 * W1700K_SDR_EXPORT_SHORT: only completions shorter than a slice leave this
 * way (radio-reduced chunks, events); the full ones are for a reader of the
 * mapping to send (mt7996-stream --send). The export thread then sleeps when
 * it has nothing to send rather than poll, so it needs no CPU of its own, and
 * an export that falls too far behind passes over what it missed and goes on
 * (export_unsent counts it) instead of ending with -ENOBUFS.
 */
#define W1700K_SDR_EXPORT_SHORT 0x1U
#define W1700K_SDR_EXPORT_FLAGS W1700K_SDR_EXPORT_SHORT
struct w1700k_sdr_stream_export {
	__u32 ifindex;		/* 0 = no export */
	__u32 src_ip, dst_ip;	/* IPv4, network byte order */
	__u16 src_port;		/* network byte order; first source port */
	__u16 dst_port;		/* network byte order */
	__u16 src_ports;	/* source port cycles over this many (0 = 1) */
	__u8 dst_mac[6];
	__u16 cpu;		/* export thread CPU + 1; 0 = any */
	__u16 flags;		/* W1700K_SDR_EXPORT_* */
	__u32 drain_ms;		/* after pumping, time allowed to send the rest: 0..W1700K_SDR_EXPORT_DRAIN_MAX */
};

/* The rest is what the export had not yet sent when the stream ended: a ring's worth at most,
 * which a link that carries the stream takes in well under a second. The wait for it is made
 * with the device held, so it is not one a caller may make as long as it likes. */
#define W1700K_SDR_EXPORT_DRAIN_MAX 60000U	/* a minute */

#define W1700K_SDR_EXPORT_MAGIC 0x31524453U	/* "SDR1" */
struct w1700k_sdr_export_header {
	__le32 magic;
	__le16 offset;		/* of this payload in the slice (always 0) */
	__le16 slice_bytes;	/* length of the completion */
	__le64 seq;		/* completion number; slice = seq % slices */
};

/*
 * STREAM_RX runs the pump on the calling thread: it consumes completions as
 * they arrive (invalidate for the CPU, publish to the header, re-arm) and
 * serves CTL requests, holding the device until it ends. timeout_ms 0 returns
 * the geometry only.
 *
 * Start: the firmware's producer is already waiting (it was entered by a
 * firmware command); if go_address is set, the driver writes go_value there.
 *
 * End: at timeout_ms, on any signal, after idle_ms without a completion (once
 * one has arrived), when the device is removed, or on a ring error, the pump
 * writes stop_value to stop_address (when set) and keeps re-arming until
 * max(idle_ms, 300 ms) pass without a completion, at most 5 s. A stream with a
 * stop word is therefore always told to stop, whatever ended it.
 *
 * go_address and stop_address are word addresses in 0x0090b000-0x0090fffc.
 * Times are CLOCK_MONOTONIC ns.
 */
#define W1700K_SDR_STREAM_TIMEOUT_MAX 604800000U	/* 7 days */
#define W1700K_SDR_STREAM_FLAGS 0x0U	/* none defined */
#define W1700K_SDR_STREAM_WORD_FIRST 0x0090b000U
#define W1700K_SDR_STREAM_WORD_LAST 0x0090fffcU

struct w1700k_sdr_stream_rx {
	__u32 timeout_ms;	/* in: 0..W1700K_SDR_STREAM_TIMEOUT_MAX */
	__u32 idle_ms;		/* in: 0 = no idle end */
	__u32 flags;		/* in: 0 (no flags are defined) */
	__u32 reserved;
	__u32 go_address, go_value;	/* in: 0 = no go word */
	__u32 stop_address, stop_value;	/* in: 0 = no stop word */
	struct w1700k_sdr_stream_export export;	/* in */
	__aligned_u64 buffers;		/* out: completions pumped */
	__aligned_u64 bytes;		/* out: sum of their lengths */
	__aligned_u64 full_buffers;	/* out: completions of >= slice_bytes / 2 */
	__aligned_u64 first_seq;	/* out: number of the first completion */
	__aligned_u64 first_ns, last_ns;	/* out: first and last completion */
	__aligned_u64 start_ns, end_ns;	/* out: pumping window */
	__aligned_u64 stop_ns;		/* out: stop word written, 0 = never */
	__u32 malformed;		/* out: completions not one well-formed packet */
	__u32 max_backlog;		/* out: most completions taken in one sweep */
	__u32 slots, slices, slice_bytes, header_bytes;	/* out: geometry */
	__s32 result;			/* out: 0, or what ended pumping */
	__s32 export_result;		/* out: 0, or what ended the export */
	__aligned_u64 export_slices;	/* out: slices sent */
	__aligned_u64 export_busy;	/* out: sends refused by a full queue */
	__aligned_u64 export_unsent;	/* out: completions never sent */
	__aligned_u64 export_last_ns;	/* out: last slice handed to the device */
	__u32 export_max_lag;		/* out: most completions waiting to be sent */
	__u32 export_inflight_max;	/* out: most datagrams queued at once */
};
#define W1700K_SDR_STREAM_RX _IOWR('Q', 0x1f, struct w1700k_sdr_stream_rx)

/*
 * CTL: word writes (in order), then word reads, in 0x0090b000-0x0090fffc,
 * whether or not a stream runs. The stream producer's RF control mailbox lives
 * there. While a pump runs it performs the request between sweeps
 * (via_pump = 1); otherwise the ioctl does it itself. Writes are posted
 * without read-back. timeout_ms bounds the wait for either.
 */
#define W1700K_SDR_CTL_MAX 64U
#define W1700K_SDR_CTL_FIRST 0x0090b000U
#define W1700K_SDR_CTL_LAST 0x0090fffcU
struct w1700k_sdr_ctl {
	__u32 count;		/* in: writes, 0..W1700K_SDR_CTL_MAX */
	__u32 read_count;	/* in: reads after them, 0..W1700K_SDR_CTL_MAX */
	__u32 read_address;	/* in: first word read */
	__u32 timeout_ms;	/* in: 1..10000 */
	__u32 address[W1700K_SDR_CTL_MAX];	/* in */
	__u32 value[W1700K_SDR_CTL_MAX];	/* in */
	__u32 read[W1700K_SDR_CTL_MAX];		/* out */
	__s32 result;		/* out: 0 or what failed */
	__u32 via_pump;		/* out: 1 when a running pump did it */
	__u32 reserved[2];
};
#define W1700K_SDR_CTL _IOWR('Q', 0x20, struct w1700k_sdr_ctl)

/*
 * LED (W1700K_SDR_CAP_LED): the front status LED's four emitters, each 0 (dark)
 * to 255, dimmed by the SoC's waveform generators at 250 Hz. It belongs to the
 * board, not to the MT7996: the call works whatever the radio and a stream are
 * doing, and the LED keeps its levels when the module is replaced. What the
 * LED says is w1700k-led's business (kernel/w1700k_sdr/ledd.h).
 */
struct w1700k_sdr_led {
	__u8 red, green, blue, white;
};
#define W1700K_SDR_LED _IOW('Q', 0x21, struct w1700k_sdr_led)
#endif
