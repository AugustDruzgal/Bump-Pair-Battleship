/*-
 * Free/Libre Near Field Communication (NFC) library
 *
 * Libnfc historical contributors:
 * Copyright (C) 2009      Roel Verdult
 * Copyright (C) 2009-2013 Romuald Conty
 * Copyright (C) 2010-2012 Romain Tartière
 * Copyright (C) 2010-2013 Philippe Teuwen
 * Copyright (C) 2012-2013 Ludovic Rousseau
 * See AUTHORS file for a more comprehensive list of contributors.
 * Additional contributors of this file:
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *  1) Redistributions of source code must retain the above copyright notice,
 *  this list of conditions and the following disclaimer.
 *  2 )Redistributions in binary form must reproduce the above copyright
 *  notice, this list of conditions and the following disclaimer in the
 *  documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
 * LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
 * CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
 * SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
 * INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
 * CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
 * ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 *
 * Note that this license only applies on the examples, NFC library itself is under LGPL
 *
 */

/**
 * @file nfc-emulate-forum-tag2.c
 * @brief Emulates a NFC-Forum Tag Type 2 with a NDEF message
 * This example allow to emulate an NFC-Forum Tag Type 2 that contains
 * a read-only NDEF message.
 *
 * This example has been developed using PN533 USB hardware as target and
 * Google Nexus S phone as initiator.
 *
 * This is know to NOT work with Nokia 6212 Classic and could fail with
 * several NFC Forum compliant devices due to the following reasons:
 *  - The emulated target has only a 4-byte UID while most devices assume a Tag
 *  Type 2 has always a 7-byte UID (as a real Mifare Ultralight tag);
 *  - The chip is emulating an ISO/IEC 14443-3 tag, without any hardware helper.
 *  If the initiator has too strict timeouts for software-based emulation
 *  (which is usually the case), this example will fail. This is not a bug
 *  and we can't do anything using this hardware (PN531/PN533).
 */

/*
 * This implementation was written based on information provided by the
 * following documents:
 *
 * NFC Forum Type 2 Tag Operation
 *  Technical Specification
 *  NFCForum-TS-Type-2-Tag_1.0 - 2007-07-09
 *
 * ISO/IEC 14443-3
 *  First edition - 2001-02-01
 *  Identification cards — Contactless integrated circuit(s) cards — Proximity cards
 *  Part 3: Initialization and anticollision
 */

#ifdef HAVE_CONFIG_H
#  include "config.h"
#endif // HAVE_CONFIG_H

#include <err.h>
#include <inttypes.h>
#include <signal.h>
#include <stdio.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#include <nfc/nfc.h>
#include <nfc/nfc-types.h>
#include <nfc/nfc-emulation.h>

#include "utils/nfc-utils.h"

static nfc_device *pnd;
static nfc_context *context;

static void stop_polling(int sig)
{
  (void) sig;
  if (pnd != NULL)
    nfc_abort_command(pnd);
  else {
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }
}

static void
print_usage(const char *progname)
{
  printf("usage: %s [-v]\n", progname);
  printf("  -v\t verbose display\n");
}

static void
stop_emulation(int sig)
{
  (void)sig;
  if (pnd != NULL) {
    nfc_abort_command(pnd);
  } else {
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }
}

#define NDEF_MEMORY_ADDR_OFFSET 25
#define BT_ADDR_LEN 17

static uint8_t __nfcforum_tag2_memory_area[] = {
  0x00, 0x00, 0x00, 0x00,  // Block 0
  0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0xFF, 0xFF,  // Block 2 (Static lock bytes: CC area and data area are read-only locked)
  0xE1, 0x10, 0x06, 0x0F,  // Block 3 (CC - NFC-Forum Tag Type 2 version 1.0, Data area (from block 4 to the end) is 48 bytes, Read-only mode)

  0x03, 24  , 0xd1, 0x01,  // Block 4 (NDEF)
  20  , 0x54, 0x02, 0x65,
  0x6E, 'A' , 'A' , ':' ,
  'B' , 'B' , ':' , 'C' ,

  'C' , ':' , 'D' , 'D' ,
  ':' , 'E' , 'E' , ':' ,
  'F' , 'F' , 0xFE, 0x00,
  0x00, 0x00, 0x00, 0x00,

  0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00,
};

#define READ 		0x30
#define WRITE 		0xA2
#define SECTOR_SELECT 	0xC2

#define HALT 		0x50
static int
nfcforum_tag2_io(struct nfc_emulator *emulator, const uint8_t *data_in, const size_t data_in_len, uint8_t *data_out, const size_t data_out_len)
{
  int res = 0;

  uint8_t *nfcforum_tag2_memory_area = (uint8_t *)(emulator->user_data);

  printf("    In: ");
  print_hex(data_in, data_in_len);

  switch (data_in[0]) {
    case READ:
      if (data_out_len >= 16) {
        memcpy(data_out, nfcforum_tag2_memory_area + (data_in[1] * 4), 16);
        res = 16;
      } else {
        res = -ENOSPC;
      }
      break;
    case HALT:
      printf("HALT sent\n");
      res = -ECONNABORTED;
      break;
    default:
      printf("Unknown command: 0x%02x\n", data_in[0]);
      res = -ENOTSUP;
  }

  if (res < 0) {
    ERR("%s (%d)", strerror(-res), -res);
  } else {
    printf("    Out: ");
    print_hex(data_out, res);
  }

  return res;
}

int nfc_emulate(char *argv[])
{
  char *BLE_MAC_ADDR = getenv("BT_ADDR");

  for (int i = 0; i < BT_ADDR_LEN; i++)
  {
    __nfcforum_tag2_memory_area[i + NDEF_MEMORY_ADDR_OFFSET] = BLE_MAC_ADDR[i];
  }

  nfc_target nt = {
    .nm = {
      .nmt = NMT_ISO14443A,
      .nbr = NBR_UNDEFINED, // Will be updated by nfc_target_init()
    },
    .nti = {
      .nai = {
        .abtAtqa = { 0x00, 0x04 },
        .abtUid = { 0x08, 0x00, 0xb0, 0x0b },
        .szUidLen = 4,
        .btSak = 0x00,
        .szAtsLen = 0,
      },
    }
  };

  struct nfc_emulation_state_machine state_machine = {
    .io = nfcforum_tag2_io
  };

  struct nfc_emulator emulator = {
    .target = &nt,
    .state_machine = &state_machine,
    .user_data = __nfcforum_tag2_memory_area,
  };

  nfc_init(&context);
  if (context == NULL) {
    ERR("Unable to init libnfc (malloc)");
    exit(EXIT_FAILURE);
  }
  pnd = nfc_open(context, NULL);

  if (pnd == NULL) {
    ERR("Unable to open NFC device");
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }

  printf("NFC device: %s opened\n", nfc_device_get_name(pnd));
  printf("Emulating NDEF tag now, please touch it with a second NFC device\n");

  if (nfc_emulate_target(pnd, &emulator, 0) < 0) {
    nfc_perror(pnd, argv[0]);
    // nfc_close(pnd);
    // nfc_exit(context);
    // exit(EXIT_FAILURE);
  }

  nfc_close(pnd);
  nfc_exit(context);

  return EXIT_SUCCESS;
}

int nfc_poll(int argc, char *argv[])
{
  bool verbose = false;

  // Display libnfc version
  const char *acLibnfcVersion = nfc_version();

  printf("%s uses libnfc %s\n", argv[0], acLibnfcVersion);
  if (argc != 1) {
    if ((argc == 2) && (0 == strcmp("-v", argv[1]))) {
      verbose = true;
    } else {
      print_usage(argv[0]);
      exit(EXIT_FAILURE);
    }
  }

  const uint8_t uiPollNr = 20;
  const uint8_t uiPeriod = 2;
  const nfc_modulation nmModulations[6] = {
    { .nmt = NMT_ISO14443A, .nbr = NBR_106 },
    { .nmt = NMT_ISO14443B, .nbr = NBR_106 },
    { .nmt = NMT_FELICA, .nbr = NBR_212 },
    { .nmt = NMT_FELICA, .nbr = NBR_424 },
    { .nmt = NMT_JEWEL, .nbr = NBR_106 },
    { .nmt = NMT_ISO14443BICLASS, .nbr = NBR_106 },
  };
  const size_t szModulations = 6;

  nfc_target nt;
  int res = 0;

  nfc_init(&context);
  if (context == NULL) {
    ERR("Unable to init libnfc (malloc)");
    exit(EXIT_FAILURE);
  }

  pnd = nfc_open(context, NULL);

  if (pnd == NULL) {
    ERR("%s", "Unable to open NFC device.");
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }

  if (nfc_initiator_init(pnd) < 0) {
    nfc_perror(pnd, "nfc_initiator_init");
    nfc_close(pnd);
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }

  printf("NFC reader: %s opened\n", nfc_device_get_name(pnd));
  printf("NFC device will poll during %ld ms (%u pollings of %lu ms for %" PRIdPTR " modulations)\n", (unsigned long) uiPollNr * szModulations * uiPeriod * 150, uiPollNr, (unsigned long) uiPeriod * 150, szModulations);
  if ((res = nfc_initiator_poll_target(pnd, nmModulations, szModulations, uiPollNr, uiPeriod, &nt))  < 0) {
    nfc_perror(pnd, "nfc_initiator_poll_target");
    nfc_close(pnd);
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }

  if (res > 0) {
    print_nfc_target(&nt, verbose);
    printf("Waiting for card removing...");
    fflush(stdout);
    while (0 == nfc_initiator_target_is_present(pnd, NULL)) {}
    nfc_perror(pnd, "nfc_initiator_target_is_present");
    printf("done.\n");
  } else {
    printf("No target found.\n");
  }

  nfc_close(pnd);
  nfc_exit(context);

  return EXIT_SUCCESS;
}

int main(int argc, char *argv[])
{
  (void)argc;
  (void)argv;

  signal(SIGINT, stop_emulation);
  signal(SIGINT, stop_polling);

  while (true)
  {
    nfc_poll(argc, argv);
    nfc_emulate(argv);
  }

  exit(EXIT_SUCCESS);
}
