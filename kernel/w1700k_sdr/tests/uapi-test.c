// SPDX-License-Identifier: GPL-2.0-only
/* Print the ABI layout as JSON: struct sizes, field offsets, ioctl numbers. */
#include <stddef.h>
#include <stdio.h>
#include "w1700k_sdr.h"

#define SIZE(s) printf("%s\"%s\":%zu", first ? "" : ",", #s, sizeof(struct s)), first = 0
#define OFF(s, f) printf(",\"%s.%s\":%zu", #s, #f, offsetof(struct s, f))
#define IOCTL(n) printf(",\"%s\":%lu", #n, (unsigned long)(n))

int main(void)
{
	int first = 1;

	printf("{");
	SIZE(w1700k_sdr_info);
	SIZE(w1700k_sdr_download);
	SIZE(w1700k_sdr_reset);
	SIZE(w1700k_sdr_mem_read);
	SIZE(w1700k_sdr_stream_ring);
	SIZE(w1700k_sdr_stream_export);
	SIZE(w1700k_sdr_led);
	SIZE(w1700k_sdr_export_header);
	SIZE(w1700k_sdr_stream_rx);
	SIZE(w1700k_sdr_ctl);
	OFF(w1700k_sdr_info, bar0_size);
	OFF(w1700k_sdr_download, result);
	OFF(w1700k_sdr_stream_ring, ctrl);
	OFF(w1700k_sdr_stream_rx, export);
	OFF(w1700k_sdr_stream_rx, buffers);
	OFF(w1700k_sdr_stream_rx, export_slices);
	OFF(w1700k_sdr_stream_export, dst_mac);
	OFF(w1700k_sdr_stream_export, drain_ms);
	OFF(w1700k_sdr_stream_export, flags);
	OFF(w1700k_sdr_led, white);
	OFF(w1700k_sdr_export_header, seq);
	OFF(w1700k_sdr_ctl, read);
	OFF(w1700k_sdr_ctl, result);
	IOCTL(W1700K_SDR_GET_INFO);
	IOCTL(W1700K_SDR_ACQUIRE_HOST);
	IOCTL(W1700K_SDR_DOWNLOAD);
	IOCTL(W1700K_SDR_RESET_MT);
	IOCTL(W1700K_SDR_READ_MEM);
	IOCTL(W1700K_SDR_STREAM_RX);
	IOCTL(W1700K_SDR_CTL);
	IOCTL(W1700K_SDR_LED);
	printf(",\"abi\":%d}\n", W1700K_SDR_ABI_VERSION);
	return 0;
}
