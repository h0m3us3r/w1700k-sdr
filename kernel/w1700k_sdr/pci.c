// SPDX-License-Identifier: GPL-2.0-only
/*
 * w1700k_sdr: MediaTek MT7996 (PCI 14c3:7990) as an SDR receiver on the
 * Gemtek W1700K.
 *
 * The driver owns the primary PCIe function: it downloads firmware, streams
 * the WM event ring into a 1 GiB DRAM ring that userspace maps read-only and
 * that can be exported as UDP, and relays word writes and reads to the
 * firmware's mailbox page. Everything is polled; no interrupt is used.
 *
 * Locking: io_lock serializes all device access and the engine session; a
 * stream pump holds it for the whole stream. map_lock orders after io_lock
 * and the mm's mmap lock and guards the stream mappings. ctl_lock (a
 * spinlock, innermost) hands CTL requests to a running pump.
 */
#include <linux/capability.h>
#include <linux/completion.h>
#include <linux/compat.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/idr.h>
#include <linux/io.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pci.h>
#include <linux/pm.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/timekeeping.h>
#include <linux/uaccess.h>
#include "w1700k_sdr.h"
#include "dma-ring.h"
#include "export.h"
#include "fwdl.h"
#include "fwdl-image.h"
#include "led.h"
#include "mmio.h"
#include "ownership.h"
#include "pump.h"
#include "wfdma.h"
#include "hw/mt7996/conn.h"
#include "hw/mt7996/host.h"

/* BAR0 must reach the L1 window selector. */
#define SDR_BAR_MIN (MT7996_HOST_L1_REMAP + 4U)
#define SDR_RINGS 3
#define SDR_CTL_TIMEOUT_MAX 10000U

struct sdr_device {
	struct pci_dev *pdev;
	void __iomem *regs;
	struct mutex io_lock;
	struct kref refs;
	struct miscdevice misc;
	struct sdr_mmio io;
	struct w1700k_sdr_info info;
	char name[32], bdf[16];
	int index;
	bool online, suspended, removing;
	/* Engine session. Once the rings are bound they stay allocated until
	 * a WiFi subsystem reset, and the module is pinned meanwhile.
	 */
	struct sdr_dma_ring *ring[SDR_RINGS];
	struct sdr_wfdma dma;
	struct sdr_fwdl *loader;
	bool dma_exposed, module_pinned;
	struct mutex map_lock;
	u32 stream_maps;
	/* CTL hand-off to a running pump. */
	spinlock_t ctl_lock;
	struct mutex ctl_serial;
	struct completion ctl_done;
	struct w1700k_sdr_ctl *ctl_req;
	bool ctl_pumping;
};

static DEFINE_IDA(sdr_ids);

static void sdr_free(struct kref *ref)
{
	struct sdr_device *sdr = container_of(ref, struct sdr_device, refs);

	ida_free(&sdr_ids, sdr->index);
	kfree(sdr);
}

static int sdr_live_locked(struct sdr_device *sdr)
{
	lockdep_assert_held(&sdr->io_lock);
	if (!sdr->online)
		return -ENODEV;
	if (sdr->suspended)
		return -EHOSTDOWN;
	if (pci_channel_offline(sdr->pdev))
		return -EIO;
	return 0;
}

/* Live and host-owned: LPCR FW_OWN_STAT clear. */
static int sdr_owned_locked(struct sdr_device *sdr)
{
	u32 owner;
	int ret = sdr_live_locked(sdr);

	if (ret)
		return ret;
	owner = readl(sdr->regs + SDR_HOST_LPCR);
	if (owner == U32_MAX)
		return -EIO;
	return owner & SDR_HOST_FW_OWN_STAT ? -EBUSY : 0;
}

static int sdr_begin(void *ctx)
{
	struct sdr_device *sdr = ctx;
	int ret = mutex_lock_interruptible(&sdr->io_lock);

	if (ret)
		return ret;
	ret = sdr_owned_locked(sdr);
	if (ret)
		mutex_unlock(&sdr->io_lock);
	return ret;
}

static void sdr_end(void *ctx)
{
	struct sdr_device *sdr = ctx;

	mutex_unlock(&sdr->io_lock);
}

static u32 sdr_read32(void *ctx, u32 bar)
{
	struct sdr_device *sdr = ctx;

	lockdep_assert_held(&sdr->io_lock);
	return readl(sdr->regs + bar);
}

static void sdr_write32(void *ctx, u32 bar, u32 value)
{
	struct sdr_device *sdr = ctx;

	lockdep_assert_held(&sdr->io_lock);
	writel(value, sdr->regs + bar);
}

static const struct sdr_mmio_ops sdr_ops = {
	.begin = sdr_begin, .end = sdr_end,
	.read32 = sdr_read32, .write32 = sdr_write32,
};

/* The ownership poll's 10-20 ms tick. */
static int sdr_own_wait(void *ctx)
{
	struct sdr_device *sdr = ctx;

	if (signal_pending(current))
		return -EINTR;
	usleep_range(10000, 20000);
	if (signal_pending(current))
		return -EINTR;
	return sdr_live_locked(sdr);
}

/* The WFDMA idle poll's 10 us tick. */
static int sdr_dma_wait(void *ctx)
{
	struct sdr_device *sdr = ctx;

	udelay(10);
	return sdr_live_locked(sdr);
}

static int sdr_acquire_host(struct sdr_device *sdr)
{
	int ret = mutex_lock_interruptible(&sdr->io_lock);

	if (ret)
		return ret;
	ret = sdr_live_locked(sdr);
	if (!ret && signal_pending(current))
		ret = -EINTR;
	if (!ret)
		ret = sdr_request_host_owned(&sdr->io, sdr_own_wait);
	mutex_unlock(&sdr->io_lock);
	return ret;
}

static int sdr_open(struct inode *inode, struct file *file)
{
	struct sdr_device *sdr = container_of(file->private_data,
					    struct sdr_device, misc);

	/* misc_open runs under misc_mtx, which remove's misc_deregister also
	 * takes, so sdr is alive here. No device lock: a stream holds it.
	 */
	if (!READ_ONCE(sdr->online))
		return -ENODEV;
	kref_get(&sdr->refs);
	file->private_data = sdr;
	return nonseekable_open(inode, file);
}

static int sdr_release(struct inode *inode, struct file *file)
{
	struct sdr_device *sdr = file->private_data;

	kref_put(&sdr->refs, sdr_free);
	return 0;
}

/* A stream mapping holds a device reference and a reference on each page,
 * so it stays memory-safe after a reset or removal frees the ring.
 */
static void sdr_vma_open(struct vm_area_struct *vma)
{
	struct sdr_device *sdr = vma->vm_private_data;

	kref_get(&sdr->refs);
	mutex_lock(&sdr->map_lock);
	sdr->stream_maps++;
	mutex_unlock(&sdr->map_lock);
}

static void sdr_vma_close(struct vm_area_struct *vma)
{
	struct sdr_device *sdr = vma->vm_private_data;

	mutex_lock(&sdr->map_lock);
	sdr->stream_maps--;
	mutex_unlock(&sdr->map_lock);
	kref_put(&sdr->refs, sdr_free);
}

static const struct vm_operations_struct sdr_vm_ops = {
	.open = sdr_vma_open, .close = sdr_vma_close,
};

static int sdr_mmap(struct file *file, struct vm_area_struct *vma)
{
	struct sdr_device *sdr = file->private_data;
	struct page **pages = NULL;
	unsigned long left;
	u32 count = 0;
	int ret = 0;

	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	if (vma->vm_pgoff || (vma->vm_flags & VM_WRITE) || !(vma->vm_flags & VM_SHARED))
		return -EINVAL;
	vm_flags_mod(vma, VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY, VM_MAYWRITE);
	mutex_lock(&sdr->map_lock);
	if (READ_ONCE(sdr->online) && sdr->ring[SDR_DMA_WM_RX])
		pages = sdr_dma_rx_pages(sdr->ring[SDR_DMA_WM_RX], &count);
	if (!pages)
		ret = -ENODEV;
	else if (vma_pages(vma) != count)
		ret = -EINVAL;
	if (!ret) {
		left = count;
		ret = vm_insert_pages(vma, vma->vm_start, pages, &left);
		if (!ret && left)
			ret = -EFAULT;
	}
	if (!ret) {
		vma->vm_ops = &sdr_vm_ops;
		vma->vm_private_data = sdr;
		kref_get(&sdr->refs);
		sdr->stream_maps++;
	}
	mutex_unlock(&sdr->map_lock);
	return ret;
}

static const char *sdr_json_bool(bool value)
{
	return value ? "true" : "false";
}

/* Status from cached state only: answers immediately during a stream. */
static ssize_t sdr_read(struct file *file, char __user *buf, size_t count, loff_t *pos)
{
	struct sdr_device *sdr = file->private_data;
	char status[256];
	int size;

	size = scnprintf(status, sizeof(status),
			 "{\"abi\":%u,\"driver\":\"w1700k_sdr\",\"pci\":\"%s\",\"online\":%s,\"suspended\":%s,\"dma_exposed\":%s,\"streaming\":%s}\n",
			 W1700K_SDR_ABI_VERSION, sdr->bdf,
			 sdr_json_bool(READ_ONCE(sdr->online)),
			 sdr_json_bool(READ_ONCE(sdr->suspended)),
			 sdr_json_bool(READ_ONCE(sdr->dma_exposed)),
			 sdr_json_bool(READ_ONCE(sdr->ctl_pumping)));
	return simple_read_from_buffer(buf, count, pos, status, size);
}

static long sdr_get_info(struct sdr_device *sdr, void __user *user)
{
	if (!READ_ONCE(sdr->online))
		return -ENODEV;
	return copy_to_user(user, &sdr->info, sizeof(sdr->info)) ? -EFAULT : 0;
}

/*
 * Bring the engine up once per session: allocate the three rings (the
 * download data and command rings and the WM event ring, which is also the
 * 1 GiB stream ring), reset WFDMA, enable bus mastering, bind the rings,
 * prepare and enable WFDMA, and open the download session. A failure leaves
 * whatever was bound in place; only a reset releases it.
 */
static int sdr_engine_up_locked(struct sdr_device *sdr)
{
	static const enum sdr_dma_queue queues[SDR_RINGS] = {
		SDR_DMA_FWDL_TX, SDR_DMA_WM_TX, SDR_DMA_WM_RX,
	};
	unsigned int i;
	int ret = 0;

	lockdep_assert_held(&sdr->io_lock);
	if (sdr->loader)
		return 0;
	mutex_lock(&sdr->map_lock);
	for (i = 0; i < SDR_RINGS && !ret; i++)
		if (!sdr->ring[i])
			ret = sdr_dma_ring_alloc(&sdr->pdev->dev, queues[i], &sdr->ring[i]);
	mutex_unlock(&sdr->map_lock);
	if (ret)
		return ret;
	ret = sdr_wfdma_init(&sdr->dma, &sdr->io, sdr_dma_wait);
	if (ret)
		return ret;
	pci_set_master(sdr->pdev);
	ret = sdr_wfdma_reset(&sdr->dma);
	if (ret)
		return ret;
	/* Binding writes ring registers: from here the device may reach the
	 * ring memory, so pin the module before the first write.
	 */
	WRITE_ONCE(sdr->dma_exposed, true);
	if (!sdr->module_pinned) {
		sdr->module_pinned = true;
		__module_get(THIS_MODULE);
		dev_info(&sdr->pdev->dev, "DMA rings bound; module pinned until reset\n");
	}
	for (i = 0; i < SDR_RINGS; i++) {
		ret = sdr_dma_ring_bind(sdr->ring[i], sdr->regs, sdr->io.bar_size);
		if (ret)
			return ret;
	}
	ret = sdr_wfdma_prepare(&sdr->dma);
	if (!ret)
		ret = sdr_wfdma_enable(&sdr->dma);
	if (!ret)
		ret = sdr_fwdl_alloc(sdr->pdev, &sdr->io, sdr->ring[SDR_DMA_FWDL_TX],
				     sdr->ring[SDR_DMA_WM_TX], sdr->ring[SDR_DMA_WM_RX],
				     &sdr->loader);
	return ret;
}

/* The executor runs under io_lock and uses read32 directly, so ownership is
 * checked here rather than by begin().
 */
static int sdr_session_locked(struct sdr_device *sdr)
{
	int ret = sdr_owned_locked(sdr);

	return ret ? ret : sdr_engine_up_locked(sdr);
}

static long sdr_do_download(struct sdr_device *sdr, void __user *user)
{
	struct w1700k_sdr_download req;
	struct sdr_fwdl_result result = {
		.sem_get_status = -1, .sem_release_status = -1,
	};
	void *image;
	int ret;

	if (copy_from_user(&req, user, sizeof(req)))
		return -EFAULT;
	if (req.reserved || !req.image || !req.bytes || req.ram_type > W1700K_SDR_RAM_DSP)
		return -EINVAL;
	if (req.bytes > SDR_FWDL_IMAGE_MAX_BYTES)
		return -EFBIG;
	image = kvmalloc(req.bytes, GFP_KERNEL);
	if (!image)
		return -ENOMEM;
	if (copy_from_user(image, u64_to_user_ptr(req.image), req.bytes)) {
		kvfree(image);
		return -EFAULT;
	}
	ret = mutex_lock_interruptible(&sdr->io_lock);
	if (!ret) {
		ret = sdr_session_locked(sdr);
		if (!ret)
			ret = sdr_fwdl_run_image(sdr->loader, image, req.bytes,
						 req.ram_type, &result);
		mutex_unlock(&sdr->io_lock);
	}
	kvfree(image);
	req.container = result.container;
	req.phase = result.phase;
	req.region = result.region;
	req.offset = result.offset;
	req.tx_submitted = result.tx_submitted;
	req.tx_completed = result.tx_completed;
	req.replies = result.replies;
	req.last_fw_state = result.last_fw_state;
	req.sem_get_status = result.sem_get_status;
	req.sem_release_status = result.sem_release_status;
	req.result = ret;
	return copy_to_user(user, &req, sizeof(req)) ? -EFAULT : ret;
}

/* Read-modify-write of one word with io_lock held, refusing a dead link. */
static int sdr_rmw_locked(struct sdr_device *sdr, u32 address, u32 clear, u32 set)
{
	struct sdr_mapping map;
	u32 value;

	lockdep_assert_held(&sdr->io_lock);
	sdr_map(address, &map);
	if (map.span < 4 || (map.bar & 3) || (u64)map.bar + 4 > sdr->io.bar_size ||
	    (map.remap_reg && (u64)map.remap_reg + 4 > sdr->io.bar_size))
		return -ERANGE;
	if (map.remap_reg) {
		value = readl(sdr->regs + map.remap_reg);
		writel((value & ~map.remap_mask) | map.remap_value,
		       sdr->regs + map.remap_reg);
		readl(sdr->regs + map.remap_reg);
	}
	value = readl(sdr->regs + map.bar);
	if (value == U32_MAX)
		return -EIO;
	writel((value & ~clear) | set, sdr->regs + map.bar);
	readl(sdr->regs + map.bar);
	return 0;
}

/* Free the session and the rings; the device must no longer reach them. */
static unsigned int sdr_session_free_locked(struct sdr_device *sdr)
{
	unsigned int i, released = 0;

	sdr_fwdl_free(sdr->loader);
	sdr->loader = NULL;
	for (i = 0; i < SDR_RINGS; i++) {
		if (!sdr->ring[i])
			continue;
		sdr_dma_ring_free_quiesced(sdr->ring[i]);
		sdr->ring[i] = NULL;
		released++;
	}
	memset(&sdr->dma, 0, sizeof(sdr->dma));
	WRITE_ONCE(sdr->dma_exposed, false);
	return released;
}

/*
 * Release the engine session. Ring memory may be freed only once the bus
 * master that could reach it is gone: stop WFDMA (observed, not required),
 * reset the whole WiFi subsystem (set, 20 ms, clear, 20 ms, as mt76 does),
 * clear bus mastering, then free. The MCU returns to its ROM download state
 * and the firmware owns band 0 again.
 */
static int sdr_engine_reset_locked(struct sdr_device *sdr, struct w1700k_sdr_reset *out)
{
	int ret;

	lockdep_assert_held(&sdr->io_lock);
	lockdep_assert_held(&sdr->map_lock);
	out->was_dma_exposed = sdr->dma_exposed;
	out->stop_observe = sdr->dma.state == SDR_WFDMA_NEW ?
			    -EALREADY : sdr_wfdma_stop_observe(&sdr->dma);
	ret = sdr_rmw_locked(sdr, MT7996_CBTOP_WF_SUBSYS_RST, 0,
			     CBTOP_WF_SUBSYS_RST_WHOLE_PATH);
	if (ret)
		return ret;
	msleep(MT7996_CBTOP_WF_SUBSYS_RST_HOLD_MS);
	ret = sdr_rmw_locked(sdr, MT7996_CBTOP_WF_SUBSYS_RST,
			     CBTOP_WF_SUBSYS_RST_WHOLE_PATH, 0);
	if (ret)
		return ret;
	msleep(MT7996_CBTOP_WF_SUBSYS_RST_HOLD_MS);
	pci_clear_master(sdr->pdev);
	out->rings_released = sdr_session_free_locked(sdr);
	out->fw_state_after = readl(sdr->regs + MT7996_TOP_MISC);
	out->owner_after = readl(sdr->regs + SDR_HOST_LPCR);
	dev_info(&sdr->pdev->dev, "WiFi subsystem reset; %u rings released\n",
		 out->rings_released);
	return 0;
}

/* The module may unload only when nothing can still call back into it: no
 * stream mapping (vm_ops) and no queued export datagram (destructor).
 */
static void sdr_unpin_locked(struct sdr_device *sdr)
{
	lockdep_assert_held(&sdr->map_lock);
	if (!sdr->module_pinned)
		return;
	if (sdr->stream_maps || sdr_export_inflight()) {
		dev_warn(&sdr->pdev->dev,
			 "stream mapping or export datagrams outstanding; module stays pinned\n");
		return;
	}
	sdr->module_pinned = false;
	module_put(THIS_MODULE);
}

static long sdr_do_reset(struct sdr_device *sdr, void __user *user)
{
	struct w1700k_sdr_reset out = {};
	int ret = mutex_lock_interruptible(&sdr->io_lock);

	if (ret)
		return ret;
	ret = sdr_live_locked(sdr);
	if (!ret) {
		mutex_lock(&sdr->map_lock);
		if (sdr->stream_maps || sdr_export_inflight())
			ret = -EBUSY;
		else
			ret = sdr_engine_reset_locked(sdr, &out);
		if (!ret)
			sdr_unpin_locked(sdr);
		mutex_unlock(&sdr->map_lock);
	}
	mutex_unlock(&sdr->io_lock);
	if (copy_to_user(user, &out, sizeof(out)))
		return -EFAULT;
	return ret;
}

static long sdr_do_mem_read(struct sdr_device *sdr, void __user *user)
{
	struct w1700k_sdr_mem_read req;
	void *data;
	int ret;

	if (copy_from_user(&req, user, sizeof(req)))
		return -EFAULT;
	if (req.reserved[0] || req.reserved[1] || !req.data || !req.length ||
	    (req.address | req.length) & 3 || req.length > SDR_MMIO_MAX_READ)
		return -EINVAL;
	if (!sdr_mem_read_allowed(req.address, req.length))
		return -EPERM;
	data = kmalloc(req.length, GFP_KERNEL);
	if (!data)
		return -ENOMEM;
	ret = sdr_mmio_read(&sdr->io, req.address, data, req.length);
	if (!ret && copy_to_user(u64_to_user_ptr(req.data), data, req.length))
		ret = -EFAULT;
	kfree(data);
	return ret;
}

/* CTL: the writes in order, then the reads, inside one held lock. */
static int sdr_ctl_apply_held(struct sdr_device *sdr, struct w1700k_sdr_ctl *c)
{
	u32 i;
	int ret = 0;

	lockdep_assert_held(&sdr->io_lock);
	for (i = 0; !ret && i < c->count; i++)
		ret = sdr_mmio_write32_held(&sdr->io, c->address[i], c->value[i]);
	for (i = 0; !ret && i < c->read_count; i++)
		ret = sdr_mmio_read32_held(&sdr->io, c->read_address + 4 * i, &c->read[i]);
	return ret;
}

/* Called by the pump with io_lock held: perform a waiting request, if any. */
static void sdr_ctl_service(struct sdr_device *sdr)
{
	struct w1700k_sdr_ctl *c;

	if (!READ_ONCE(sdr->ctl_req))
		return;
	spin_lock(&sdr->ctl_lock);
	c = sdr->ctl_req;
	sdr->ctl_req = NULL;
	spin_unlock(&sdr->ctl_lock);
	if (!c)
		return;
	c->result = sdr_ctl_apply_held(sdr, c);
	c->via_pump = 1;
	complete(&sdr->ctl_done);
}

static void sdr_ctl_pumping(struct sdr_device *sdr, bool on)
{
	spin_lock(&sdr->ctl_lock);
	WRITE_ONCE(sdr->ctl_pumping, on);
	spin_unlock(&sdr->ctl_lock);
}

static bool sdr_ctl_word(u32 address)
{
	return !(address & 3) && address >= W1700K_SDR_CTL_FIRST &&
	       address <= W1700K_SDR_CTL_LAST;
}

static int sdr_ctl_check(const struct w1700k_sdr_ctl *c)
{
	u32 i;

	if (c->reserved[0] || c->reserved[1] || c->count > W1700K_SDR_CTL_MAX ||
	    c->read_count > W1700K_SDR_CTL_MAX || !c->timeout_ms ||
	    c->timeout_ms > SDR_CTL_TIMEOUT_MAX || (!c->count && !c->read_count))
		return -EINVAL;
	for (i = 0; i < c->count; i++)
		if (!sdr_ctl_word(c->address[i]))
			return -EINVAL;
	if (c->read_count && (!sdr_ctl_word(c->read_address) ||
			      (u64)c->read_address + 4 * (c->read_count - 1) >
			      W1700K_SDR_CTL_LAST))
		return -EINVAL;
	return 0;
}

/* Hand the request to the running pump and wait for it. */
static int sdr_ctl_via_pump(struct sdr_device *sdr, struct w1700k_sdr_ctl *c,
			    unsigned long deadline)
{
	long left;

	reinit_completion(&sdr->ctl_done);
	sdr->ctl_req = c;
	spin_unlock(&sdr->ctl_lock);
	left = wait_for_completion_interruptible_timeout(&sdr->ctl_done,
		time_after(deadline, jiffies) ? deadline - jiffies : 1);
	if (left > 0)
		return c->result;
	spin_lock(&sdr->ctl_lock);
	if (sdr->ctl_req == c) {
		sdr->ctl_req = NULL;
		spin_unlock(&sdr->ctl_lock);
		return left < 0 ? (int)left : -ETIMEDOUT;
	}
	spin_unlock(&sdr->ctl_lock);
	/* The pump has taken it; c must outlive its use. */
	wait_for_completion(&sdr->ctl_done);
	return c->result;
}

static long sdr_do_ctl(struct sdr_device *sdr, void __user *user)
{
	struct w1700k_sdr_ctl *c;
	unsigned long deadline;
	long out;
	int ret;

	c = kzalloc(sizeof(*c), GFP_KERNEL);
	if (!c)
		return -ENOMEM;
	ret = copy_from_user(c, user, sizeof(*c)) ? -EFAULT : sdr_ctl_check(c);
	if (ret)
		goto done;
	c->via_pump = 0;
	deadline = jiffies + msecs_to_jiffies(c->timeout_ms);
	ret = mutex_lock_interruptible(&sdr->ctl_serial);
	if (ret)
		goto done;
	for (;;) {
		spin_lock(&sdr->ctl_lock);
		if (sdr->ctl_pumping) {
			ret = sdr_ctl_via_pump(sdr, c, deadline);	/* unlocks */
			break;
		}
		spin_unlock(&sdr->ctl_lock);
		if (mutex_trylock(&sdr->io_lock)) {
			ret = sdr_owned_locked(sdr);
			if (!ret)
				ret = sdr_ctl_apply_held(sdr, c);
			mutex_unlock(&sdr->io_lock);
			break;
		}
		if (time_after(jiffies, deadline)) {
			ret = -ETIMEDOUT;
			break;
		}
		if (msleep_interruptible(1)) {
			ret = -EINTR;
			break;
		}
	}
	mutex_unlock(&sdr->ctl_serial);
done:
	c->result = ret;
	out = copy_to_user(user, c, sizeof(*c)) ? -EFAULT : ret;
	kfree(c);
	return out;
}

static_assert(sizeof(struct w1700k_sdr_stream_ring) == sizeof(struct sdr_dma_stream_header));
static_assert(offsetof(struct w1700k_sdr_stream_ring, ctrl) ==
	      offsetof(struct sdr_dma_stream_header, ctrl));

static bool sdr_stream_word_ok(u32 address)
{
	return !address || (!(address & 3) && address >= W1700K_SDR_STREAM_WORD_FIRST &&
			    address <= W1700K_SDR_STREAM_WORD_LAST);
}

static int sdr_stream_check(const struct w1700k_sdr_stream_rx *req)
{
	if (req->reserved || (req->flags & ~W1700K_SDR_STREAM_FLAGS) ||
	    req->timeout_ms > W1700K_SDR_STREAM_TIMEOUT_MAX ||
	    req->export.drain_ms > W1700K_SDR_STREAM_TIMEOUT_MAX ||
	    !sdr_stream_word_ok(req->go_address) ||
	    !sdr_stream_word_ok(req->stop_address))
		return -EINVAL;
	return 0;
}

/* Release the waiting producer with the go word. */
static int sdr_stream_start_locked(struct sdr_device *sdr,
				   const struct w1700k_sdr_stream_rx *req)
{
	u32 before, after;

	if (!req->go_address)
		return 0;
	return sdr_mmio_rmw_held(&sdr->io, req->go_address, U32_MAX, req->go_value,
				 &before, &after);
}

/* The pump's view of the device; io_lock is held throughout. */
struct sdr_pump_ctx {
	struct sdr_device *sdr;
	struct sdr_dma_ring *rx;
};

static int sdr_pump_next(void *ctx, u32 *ctrl, u64 *seq)
{
	struct sdr_pump_ctx *p = ctx;

	return sdr_dma_rx_next(p->rx, ctrl, seq);
}

static int sdr_pump_stop(void *ctx, u32 address, u32 value)
{
	struct sdr_pump_ctx *p = ctx;
	u32 before, after;

	return sdr_mmio_rmw_held(&p->sdr->io, address, U32_MAX, value, &before, &after);
}

static void sdr_pump_service(void *ctx, bool idle)
{
	struct sdr_pump_ctx *p = ctx;

	sdr_ctl_service(p->sdr);
	if (idle)
		cond_resched();
}

static bool sdr_pump_interrupted(void *ctx, bool *fatal)
{
	struct sdr_pump_ctx *p = ctx;

	*fatal = fatal_signal_pending(current);
	return signal_pending(current) || READ_ONCE(p->sdr->removing);
}

static u64 sdr_pump_now(void *ctx)
{
	return ktime_get_ns();
}

static const struct sdr_pump_ops sdr_pump_ops = {
	.next = sdr_pump_next, .stop = sdr_pump_stop, .service = sdr_pump_service,
	.interrupted = sdr_pump_interrupted, .now = sdr_pump_now,
};

static long sdr_do_stream_rx(struct sdr_device *sdr, void __user *user)
{
	struct w1700k_sdr_stream_rx req;
	struct sdr_dma_stream_header_view view;
	const struct sdr_dma_stream_header *h;
	struct sdr_export x = {};
	struct sdr_dma_ring *rx;
	struct page **pages;
	u32 count;
	int ret;

	if (copy_from_user(&req, user, sizeof(req)))
		return -EFAULT;
	ret = sdr_stream_check(&req);
	if (ret)
		return ret;
	memset(&req.buffers, 0, sizeof(req) - offsetof(struct w1700k_sdr_stream_rx, buffers));
	ret = mutex_lock_interruptible(&sdr->io_lock);
	if (ret)
		goto report;
	ret = sdr_session_locked(sdr);
	if (!ret)
		ret = sdr_fwdl_stream_begin(sdr->loader, &rx);
	if (ret)
		goto unlock;
	pages = sdr_dma_rx_pages(rx, &count);
	if (!pages) {
		ret = -ENODEV;
		goto end;
	}
	h = page_address(pages[0]);
	req.slots = h->slots;
	req.slices = h->slices;
	req.slice_bytes = h->slice_bytes;
	req.header_bytes = h->header_bytes;
	if (!req.timeout_ms)
		goto end;
	view = (struct sdr_dma_stream_header_view){
		&h->completed, h->ctrl, h->slots, h->slices, h->slice_bytes,
		h->header_bytes,
	};
	sdr_ctl_pumping(sdr, true);
	ret = sdr_export_open(&x, &req.export, pages, &view);
	if (!ret)
		ret = sdr_stream_start_locked(sdr, &req);
	if (!ret) {
		struct sdr_pump_ctx pump = { sdr, rx };

		ret = sdr_pump_run(&sdr_pump_ops, &pump, &req);
	}
	req.end_ns = ktime_get_ns();
	sdr_export_finish(&x, req.first_seq + req.buffers, &req);
	sdr_ctl_pumping(sdr, false);
	sdr_ctl_service(sdr);
end:
	sdr_fwdl_stream_end(sdr->loader);
unlock:
	mutex_unlock(&sdr->io_lock);
report:
	req.result = ret;
	return copy_to_user(user, &req, sizeof(req)) ? -EFAULT : ret;
}

/* The status LED belongs to the board, not to an MT7996: one for the module, and usable
 * whatever the radio is doing (no io_lock).
 */
static struct sdr_led sdr_led;

static long sdr_do_led(void __user *user)
{
	struct w1700k_sdr_led req;

	if (copy_from_user(&req, user, sizeof(req)))
		return -EFAULT;
	return sdr_led_set(&sdr_led, &req);
}

static bool sdr_privileged_ioctl(unsigned int cmd)
{
	switch (cmd) {
	case W1700K_SDR_ACQUIRE_HOST:
	case W1700K_SDR_DOWNLOAD:
	case W1700K_SDR_RESET_MT:
	case W1700K_SDR_READ_MEM:
	case W1700K_SDR_STREAM_RX:
	case W1700K_SDR_CTL:
	case W1700K_SDR_LED:
		return true;
	}
	return false;
}

static long sdr_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct sdr_device *sdr = file->private_data;
	void __user *user = (void __user *)arg;

	if (cmd == W1700K_SDR_GET_INFO)
		return sdr_get_info(sdr, user);
	if (!sdr_privileged_ioctl(cmd))
		return -ENOTTY;
	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	switch (cmd) {
	case W1700K_SDR_ACQUIRE_HOST:
		return arg ? -EINVAL : sdr_acquire_host(sdr);
	case W1700K_SDR_DOWNLOAD:
		return sdr_do_download(sdr, user);
	case W1700K_SDR_RESET_MT:
		return sdr_do_reset(sdr, user);
	case W1700K_SDR_READ_MEM:
		return sdr_do_mem_read(sdr, user);
	case W1700K_SDR_STREAM_RX:
		return sdr_do_stream_rx(sdr, user);
	case W1700K_SDR_CTL:
		return sdr_do_ctl(sdr, user);
	case W1700K_SDR_LED:
		return sdr_do_led(user);
	}
	return -ENOTTY;
}

static const struct file_operations sdr_fops = {
	.owner = THIS_MODULE, .open = sdr_open, .release = sdr_release,
	.read = sdr_read, .unlocked_ioctl = sdr_ioctl, .mmap = sdr_mmap,
#ifdef CONFIG_COMPAT
	.compat_ioctl = compat_ptr_ioctl,
#endif
};

/*
 * ASPM L0s/L1 cuts the device's DMA write rate to about 0.33 GB/s (1.44 GB/s
 * without); the stream needs 1.28 GB/s.
 */
static void sdr_disable_aspm(struct pci_dev *pdev)
{
	struct pci_dev *port = pci_upstream_bridge(pdev);
	u16 link = 0, upstream = 0;
	int ret;

	ret = pci_disable_link_state(pdev, PCIE_LINK_STATE_L0S | PCIE_LINK_STATE_L1);
	pcie_capability_read_word(pdev, PCI_EXP_LNKCTL, &link);
	if (port)
		pcie_capability_read_word(port, PCI_EXP_LNKCTL, &upstream);
	if (ret || (link & PCI_EXP_LNKCTL_ASPMC) || (upstream & PCI_EXP_LNKCTL_ASPMC))
		dev_warn(&pdev->dev,
			 "ASPM not disabled (%d); link control %#06x, upstream %#06x\n",
			 ret, link, upstream);
	else
		dev_info(&pdev->dev, "ASPM disabled; link control %#06x, upstream %#06x\n",
			 link, upstream);
}

static int sdr_probe(struct pci_dev *pdev, const struct pci_device_id *id)
{
	struct sdr_device *sdr;
	int ret;

	/* The secondary PCIe interface (and dynamic ids) are not ours. */
	if (pdev->vendor != MT7996_PCI_VENDOR_ID || pdev->device != MT7996_PCI_DEVICE_ID) {
		dev_info(&pdev->dev, "not the MT7996 primary interface (%04x:%04x)\n",
			 pdev->vendor, pdev->device);
		return -ENODEV;
	}
	if (!(pci_resource_flags(pdev, 0) & IORESOURCE_MEM) ||
	    pci_resource_len(pdev, 0) < SDR_BAR_MIN) {
		dev_warn(&pdev->dev, "BAR0 unusable: flags %#lx, length %llu, need %u\n",
			 (unsigned long)pci_resource_flags(pdev, 0),
			 (unsigned long long)pci_resource_len(pdev, 0),
			 (unsigned int)SDR_BAR_MIN);
		return -ENODEV;
	}
	ret = pci_enable_device_mem(pdev);
	if (ret) {
		dev_warn(&pdev->dev, "pci_enable_device_mem failed: %d\n", ret);
		return ret;
	}
	/* Bus mastering stays off until the first download binds the rings. */
	pci_clear_master(pdev);
	sdr_disable_aspm(pdev);
	ret = pci_request_region(pdev, 0, "w1700k_sdr");
	if (ret) {
		dev_warn(&pdev->dev, "BAR0 region busy: %d\n", ret);
		goto disable;
	}
	sdr = kzalloc(sizeof(*sdr), GFP_KERNEL);
	if (!sdr) {
		ret = -ENOMEM;
		goto region;
	}
	sdr->index = ida_alloc(&sdr_ids, GFP_KERNEL);
	if (sdr->index < 0) {
		ret = sdr->index;
		kfree(sdr);
		goto region;
	}
	kref_init(&sdr->refs);
	mutex_init(&sdr->io_lock);
	mutex_init(&sdr->map_lock);
	spin_lock_init(&sdr->ctl_lock);
	mutex_init(&sdr->ctl_serial);
	init_completion(&sdr->ctl_done);
	sdr->pdev = pdev;
	sdr->regs = pci_iomap(pdev, 0, 0);
	if (!sdr->regs) {
		ret = -ENOMEM;
		goto free;
	}
	sdr->io = (struct sdr_mmio){ &sdr_ops, sdr, pci_resource_len(pdev, 0) };
	sdr->info = (struct w1700k_sdr_info){
		.abi_version = W1700K_SDR_ABI_VERSION,
		.capabilities = W1700K_SDR_CAP_HOST_OWN | W1700K_SDR_CAP_DOWNLOAD |
				W1700K_SDR_CAP_RESET | W1700K_SDR_CAP_MEM |
				W1700K_SDR_CAP_STREAM_RX | W1700K_SDR_CAP_CTL | W1700K_SDR_CAP_LED,
		.vendor = pdev->vendor, .device = pdev->device, .revision = pdev->revision,
		.max_read = SDR_MMIO_MAX_READ, .bar0_size = sdr->io.bar_size,
	};
	strscpy(sdr->bdf, pci_name(pdev), sizeof(sdr->bdf));
	if (sdr->index)
		snprintf(sdr->name, sizeof(sdr->name), "w1700k-sdr%d", sdr->index);
	else
		strscpy(sdr->name, "w1700k-sdr", sizeof(sdr->name));
	sdr->misc = (struct miscdevice){
		.minor = MISC_DYNAMIC_MINOR, .name = sdr->name,
		.fops = &sdr_fops, .mode = 0600, .parent = &pdev->dev,
	};
	sdr->online = true;
	pci_set_drvdata(pdev, sdr);
	ret = misc_register(&sdr->misc);
	if (!ret) {
		dev_info(&pdev->dev, "MT7996 SDR on /dev/%s\n", sdr->name);
		return 0;
	}
	pci_set_drvdata(pdev, NULL);
	pci_iounmap(pdev, sdr->regs);
free:
	kref_put(&sdr->refs, sdr_free);
region:
	pci_release_region(pdev, 0);
disable:
	pci_disable_device(pdev);
	return ret;
}

/*
 * Removal ends a running stream (the pump sees `removing`), then resets the
 * WiFi subsystem before the BAR is unmapped, so the device can no longer
 * reach the rings that are freed.
 */
static void sdr_remove(struct pci_dev *pdev)
{
	struct sdr_device *sdr = pci_get_drvdata(pdev);
	struct w1700k_sdr_reset out = {};
	int ret;

	WRITE_ONCE(sdr->removing, true);
	mutex_lock(&sdr->io_lock);
	WRITE_ONCE(sdr->online, false);
	mutex_lock(&sdr->map_lock);
	if (sdr->dma_exposed) {
		ret = sdr_engine_reset_locked(sdr, &out);
		if (ret)
			dev_err(&pdev->dev,
				"reset failed (%d); DMA rings leaked, module stays pinned\n",
				ret);
		else
			sdr_unpin_locked(sdr);
	} else {
		sdr_session_free_locked(sdr);	/* never bound */
	}
	mutex_unlock(&sdr->map_lock);
	mutex_unlock(&sdr->io_lock);
	/* Not under io_lock: misc_open takes misc_mtx before sdr_open. */
	misc_deregister(&sdr->misc);
	pci_iounmap(pdev, sdr->regs);
	pci_release_region(pdev, 0);
	pci_disable_device(pdev);
	pci_set_drvdata(pdev, NULL);
	kref_put(&sdr->refs, sdr_free);
}

static int sdr_suspend(struct device *dev)
{
	struct sdr_device *sdr = dev_get_drvdata(dev);

	mutex_lock(&sdr->io_lock);
	WRITE_ONCE(sdr->suspended, true);
	mutex_unlock(&sdr->io_lock);
	return 0;
}

static int sdr_resume(struct device *dev)
{
	struct sdr_device *sdr = dev_get_drvdata(dev);

	mutex_lock(&sdr->io_lock);
	WRITE_ONCE(sdr->suspended, false);
	mutex_unlock(&sdr->io_lock);
	return 0;
}

static DEFINE_SIMPLE_DEV_PM_OPS(sdr_pm, sdr_suspend, sdr_resume);
static const struct pci_device_id sdr_ids_table[] = {
	{ PCI_DEVICE(MT7996_PCI_VENDOR_ID, MT7996_PCI_DEVICE_ID) },
	{ }
};
MODULE_DEVICE_TABLE(pci, sdr_ids_table);

static struct pci_driver sdr_driver = {
	.name = "w1700k_sdr", .id_table = sdr_ids_table,
	.probe = sdr_probe, .remove = sdr_remove,
	.driver.pm = pm_sleep_ptr(&sdr_pm),
};
static int __init sdr_init(void)
{
	int ret;

	sdr_led_init(&sdr_led);		/* without it the receiver works all the same */
	ret = pci_register_driver(&sdr_driver);
	if (ret)
		sdr_led_exit(&sdr_led);
	return ret;
}

static void __exit sdr_exit(void)
{
	pci_unregister_driver(&sdr_driver);
	sdr_led_exit(&sdr_led);
}

module_init(sdr_init);
module_exit(sdr_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("MediaTek MT7996 SDR receiver driver for the Gemtek W1700K");
