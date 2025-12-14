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
 * @file nfc-dep-initiator.c
 * @brief Turns the NFC device into a D.E.P. initiator (see NFCIP-1)
 */

#ifdef HAVE_CONFIG_H
#  include "config.h"
#endif // HAVE_CONFIG_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <pthread.h>
#include <unistd.h>
#include <fcntl.h>
#include <err.h>
#include <inttypes.h>
#include <stddef.h>
#include <errno.h>
#include <sys/types.h>
#include <sys/stat.h>

#include <time.h>
#include <nfc/nfc.h>

#include "utils/nfc-utils.h"

#define MAX_FRAME_LEN 264
#define BT_ADDR_LEN 17

static nfc_device *pnd;
static nfc_context *context;

bool waiting = false;
bool kill_ithread = false;

int fd;
pthread_t thread_id;

void* interrupt_thread_func(void* arg)
{
    while (true)
    {
      while (!waiting)
      {
        if (kill_ithread)
        {
          return NULL;
        }

        usleep(25000);
      }

      int sleep_duration = 2500000 + (rand()%500000);
      usleep(sleep_duration);

      if (pnd != NULL && waiting)
      {
        nfc_abort_command(pnd);
      }
    }

    return NULL;
}

static void stop_dep_communication(int sig)
{
  (void) sig;
  if (pnd != NULL) {
    nfc_abort_command(pnd);
  } else {
    nfc_exit(context);
    exit(EXIT_FAILURE);
  }
}

static void signal_terminate(int sig)
{
  (void) sig;

  kill_ithread = true;

  pthread_join(thread_id, NULL);
  close(fd);

  if (pnd != NULL) {
    nfc_abort_command(pnd);
  }

  nfc_close(pnd);
  nfc_exit(context);
  exit(EXIT_SUCCESS);
}

void open_addr_fifo(void)
{
  char *addr_fifo = getenv("BT_ADDR_PIPE");
  
  mkfifo(addr_fifo, 0666);
  
  fd = open(addr_fifo, O_WRONLY);

  if (fd < 0) 
  {
    perror("open fifo error");
    return;
  }
}

void send_addr_to_fifo(uint8_t *addr, int len)
{
  char flush_chrc = '\n';
  
  write(fd, addr, len);

  // Write a new line to flush the pipe
  write(fd, &flush_chrc, 1);
}

void close_addr_fifo(void)
{
  close(fd);
}

int nfc_dep_initiator(int argc, const char *argv[])
{
  nfc_target nt;
  uint8_t  abtRx[MAX_FRAME_LEN];
  uint8_t *abtTx = (uint8_t *) getenv("BT_ADDR");

  if (argc > 1) {
    printf("Usage: %s\n", argv[0]);
    return EXIT_FAILURE;;
  }

  nfc_init(&context);
  if (context == NULL) {
    ERR("Unable to init libnfc (malloc)");
    return EXIT_FAILURE;;
  }

  pnd = nfc_open(context, NULL);
  if (pnd == NULL) {
    ERR("Unable to open NFC device.");
    nfc_exit(context);
    return EXIT_FAILURE;;
  }
  printf("NFC device: intiator mode\n");
  fflush(stdout);

  if (nfc_initiator_init(pnd) < 0) {
    nfc_perror(pnd, "nfc_initiator_init");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;;
  }

  if (nfc_initiator_select_dep_target(pnd, NDM_PASSIVE, NBR_212, NULL, &nt, 1000) < 0) {
    nfc_perror(pnd, "nfc_initiator_select_dep_target");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;;
  }
  print_nfc_target(&nt, false);

  printf("Sending: %s\n", abtTx);
  int res;
  if ((res = nfc_initiator_transceive_bytes(pnd, abtTx, strlen((char *) abtTx), abtRx, sizeof(abtRx), 0)) < 0) {
    nfc_perror(pnd, "nfc_initiator_transceive_bytes");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;;
  }

  abtRx[res] = 0;
  printf("Received: %s\n", abtRx);

  if (nfc_initiator_deselect_target(pnd) < 0) {
    nfc_perror(pnd, "nfc_initiator_deselect_target");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;;
  }

  nfc_close(pnd);
  nfc_exit(context);
  return EXIT_SUCCESS;;
}

int nfc_dep_target(int argc, const char *argv[])
{
  uint8_t  abtRx[MAX_FRAME_LEN];
  int  szRx;
  uint8_t  abtTx[] = "Address received!";

  if (argc > 1) {
    printf("Usage: %s\n", argv[0]);
    return EXIT_FAILURE;
  }

  nfc_init(&context);
  if (context == NULL) {
    ERR("Unable to init libnfc (malloc)");
    return EXIT_FAILURE;
  }
#define MAX_DEVICE_COUNT 2
  nfc_connstring connstrings[MAX_DEVICE_COUNT];
  size_t szDeviceFound = nfc_list_devices(context, connstrings, MAX_DEVICE_COUNT);
  // Little hack to allow using nfc-dep-initiator & nfc-dep-target from
  // the same machine: if there is more than one readers opened
  // nfc-dep-target will open the second reader
  // (we hope they're always detected in the same order)
  if (szDeviceFound == 1) {
    pnd = nfc_open(context, connstrings[0]);
  } else if (szDeviceFound > 1) {
    pnd = nfc_open(context, connstrings[1]);
  } else {
    printf("No device found.\n");
    nfc_exit(context);
    return EXIT_FAILURE;
  }

  nfc_target nt = {
    .nm = {
      .nmt = NMT_DEP,
      .nbr = NBR_UNDEFINED
    },
    .nti = {
      .ndi = {
        .abtNFCID3 = { 0x12, 0x34, 0x56, 0x78, 0x9a, 0xbc, 0xde, 0xff, 0x00, 0x00 },
        .szGB = 4,
        .abtGB = { 0x12, 0x34, 0x56, 0x78 },
        .ndm = NDM_UNDEFINED,
        /* These bytes are not used by nfc_target_init: the chip will provide them automatically to the initiator */
        .btDID = 0x00,
        .btBS = 0x00,
        .btBR = 0x00,
        .btTO = 0x00,
        .btPP = 0x01,
      },
    },
  };

  if (pnd == NULL) {
    printf("Unable to open NFC device.\n");
    nfc_exit(context);
    return EXIT_FAILURE;
  }
  printf("NFC device: %s opened\n", nfc_device_get_name(pnd));

  waiting = true;

  printf("NFC device: target mode\n");
  fflush(stdout);
  // print_nfc_target(&nt, false);

  printf("Waiting for initiator request...\n");
  if ((szRx = nfc_target_init(pnd, &nt, abtRx, sizeof(abtRx), 0)) < 0) {
    nfc_perror(pnd, "nfc_target_init");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;
  }

  printf("Initiator request received. Waiting for data...\n");
  if ((szRx = nfc_target_receive_bytes(pnd, abtRx, sizeof(abtRx), 0)) < 0) {
    nfc_perror(pnd, "nfc_target_receive_bytes");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;
  }
  abtRx[(size_t) szRx] = '\0';
  printf("Received: %s\n", abtRx);

  if (szRx >= BT_ADDR_LEN)
  {
    send_addr_to_fifo(abtRx, szRx);
  }

  printf("Sending: %s\n", abtTx);
  if (nfc_target_send_bytes(pnd, abtTx, sizeof(abtTx), 0) < 0) {
    nfc_perror(pnd, "nfc_target_send_bytes");
    nfc_close(pnd);
    nfc_exit(context);
    return EXIT_FAILURE;
  }
  printf("Data sent.\n");

  nfc_close(pnd);
  nfc_exit(context);
  
  return EXIT_SUCCESS;
}



int main(int argc, const char *argv[])
{
  open_addr_fifo();
  
  signal(SIGINT, stop_dep_communication);
  signal(SIGTERM, signal_terminate);

  srand(time(NULL));

  // Create the thread
  if (pthread_create(&thread_id, NULL, interrupt_thread_func, NULL) != 0) {
      perror("Failed to create thread");
      return 1;
  }

  while (true)
  {
    waiting = false;
    nfc_dep_initiator(argc, argv);
    nfc_dep_target(argc, argv);
  }
}
