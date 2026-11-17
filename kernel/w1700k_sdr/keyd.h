/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * The paperclip key: what the reset button means and what a login key must look like. The logic of
 * w1700k-key (keyd.c), kept apart from its I/O so that tests can run it (tests/key_check.c), and
 * shared with the LED (ledd.h), which shows the same thresholds.
 *
 * The unit has one slot for a login key that only a hand on the unit can change:
 *
 *   held KEY_TOGGLE_SECONDS, released     the slot's key is switched off, or on again
 *   held KEY_PROGRAM_SECONDS, released    programming: for KEY_WINDOW_SECONDS the unit takes one
 *                                         public key on its management address. A press in that
 *                                         time ends it
 *   held KEY_PURGE_SECONDS, released      the slot's key is erased
 *
 * What a press means is decided when the button is released, by how long it was held; a shorter
 * press means nothing.
 *
 * A key that arrives is held, not kept: the network may offer one, only the hand takes it.
 *
 *   held KEY_CONFIRM_SECONDS, released    the key that arrived goes into the slot
 *   held KEY_APPLY_SECONDS                the same, there and then: the hand has said yes for
 *                                         long enough, and letting go means nothing more
 *   a shorter press                       it is discarded
 *   KEY_PENDING_SECONDS with no press     it is discarded
 *
 * Either way that was the one key of this programming: another needs the button held again.
 */
#ifndef W1700K_KEYD_H
#define W1700K_KEYD_H
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define KEY_TOGGLE_SECONDS	5.0
#define KEY_PROGRAM_SECONDS	10.0
#define KEY_PURGE_SECONDS	15.0
#define KEY_WINDOW_SECONDS	180.0
#define KEY_CONFIRM_SECONDS	3.0
#define KEY_APPLY_SECONDS	5.0
#define KEY_PENDING_SECONDS	60.0
#define KEY_PORT		7722
#define KEY_LINE_MAX		1100	/* a 4096-bit RSA key and a comment */
#define KEY_BLOB_MAX		800	/* base64 characters */

enum key_action {
	KEY_ACT_NONE,
	KEY_ACT_TOGGLE,
	KEY_ACT_PROGRAM,
	KEY_ACT_PURGE,
};

static inline enum key_action key_action_for(double held, double toggle, double program, double purge)
{
	return held >= purge ? KEY_ACT_PURGE : held >= program ? KEY_ACT_PROGRAM :
	       held >= toggle ? KEY_ACT_TOGGLE : KEY_ACT_NONE;
}

/* The slot's state, as w1700k-keyslot writes it and the loader passes it. */
enum key_slot {
	KEY_SLOT_EMPTY,
	KEY_SLOT_OFF,
	KEY_SLOT_ON,
};

static inline enum key_slot key_slot_named(const char *word)
{
	return !strncmp(word, "on", 2) ? KEY_SLOT_ON : !strncmp(word, "off", 3) ? KEY_SLOT_OFF : KEY_SLOT_EMPTY;
}

static inline int key_b64_value(char c)
{
	return c >= 'A' && c <= 'Z' ? c - 'A' : c >= 'a' && c <= 'z' ? c - 'a' + 26 :
	       c >= '0' && c <= '9' ? c - '0' + 52 : c == '+' ? 62 : c == '/' ? 63 : -1;
}

/* Strict base64: whole quads, padding only at the end. The decoded length, or -1. */
static inline int key_b64(const char *in, size_t n, unsigned char *out, size_t cap)
{
	size_t i, used = 0;

	if (!n || n % 4)
		return -1;
	for (i = 0; i < n; i += 4) {
		int pad = (in[i + 2] == '=') + (in[i + 3] == '='), k, v[4];

		for (k = 0; k < 4; k++)
			v[k] = k >= 4 - pad ? 0 : key_b64_value(in[i + k]);
		if (v[0] < 0 || v[1] < 0 || v[2] < 0 || v[3] < 0 || (pad && i + 4 != n) ||
		    (in[i + 2] == '=' && in[i + 3] != '=') || used + 3 - pad > cap)
			return -1;
		out[used++] = (unsigned char)(v[0] << 2 | v[1] >> 4);
		if (pad < 2)
			out[used++] = (unsigned char)(v[1] << 4 | v[2] >> 2);
		if (pad < 1)
			out[used++] = (unsigned char)(v[2] << 6 | v[3]);
	}
	return (int)used;
}

/*
 * One line of a public key file, "TYPE BASE64 [comment]", checked and written as the slot holds
 * it: "TYPE:BASE64", which has no space and nothing a shell or a kernel command line reads as
 * more than a word. NULL when it is good, else what is wrong with it.
 *
 * A key is good when its type is one the unit's ssh server takes, its body is base64, and the
 * body itself begins with the same type, as every SSH public key does: that catches a truncated
 * paste, a private key, and a line from some other file.
 */
static inline const char *key_line_check(const char *line, char *slot, size_t cap)
{
	static const char *const types[] = { "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
					     "ecdsa-sha2-nistp521", "ssh-rsa" };
	unsigned char body[KEY_BLOB_MAX];
	size_t type_len, blob_len, k;
	const char *blob;
	int n;

	while (*line == ' ' || *line == '\t')
		line++;
	for (k = 0; line[k]; k++)
		if ((unsigned char)line[k] < 0x20 && line[k] != '\t' && line[k] != '\r' && line[k] != '\n')
			return "not a public key: it holds control characters";
	type_len = strcspn(line, " \t");
	for (k = 0; k < sizeof(types) / sizeof(types[0]); k++)
		if (strlen(types[k]) == type_len && !memcmp(types[k], line, type_len))
			break;
	if (k == sizeof(types) / sizeof(types[0]))
		return strstr(line, "PRIVATE KEY") ? "that is a private key: send the .pub file" :
		       "not a public key this unit takes (ssh-ed25519, ecdsa-sha2-nistp256/384/521, ssh-rsa)";
	blob = line + type_len;
	while (*blob == ' ' || *blob == '\t')
		blob++;
	blob_len = strcspn(blob, " \t\r\n");
	if (!blob_len || blob_len > KEY_BLOB_MAX)
		return "not a public key: its body is missing or too long";
	n = key_b64(blob, blob_len, body, sizeof(body));
	if (n < 0)
		return "not a public key: its body is not base64 (cut short?)";
	if ((size_t)n < 4 + type_len + 4 || body[0] || body[1] || body[2] || body[3] != type_len ||
	    memcmp(body + 4, line, type_len))
		return "not a public key: its body is not a key of the type it names";
	if (!strncmp(line, "ssh-ed25519", type_len) && n != 4 + 11 + 4 + 32)
		return "not a public key: an Ed25519 key of the wrong length (cut short?)";
	if (type_len + 1 + blob_len + 1 > cap)
		return "not a public key: too long";
	memcpy(slot, line, type_len);
	slot[type_len] = ':';
	memcpy(slot + type_len + 1, blob, blob_len);
	slot[type_len + 1 + blob_len] = 0;
	return NULL;
}

#endif
