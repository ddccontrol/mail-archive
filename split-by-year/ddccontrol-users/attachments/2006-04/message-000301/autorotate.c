/*
 * Copyright (c) 2006  Daniel Elstner <daniel.kitta@...157...>
 *
 * This program is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
 *
 * Build command:
 *
 * gcc -g -O2 `pkg-config --cflags --libs libxml-2.0 gdk-2.0`
 *     -I/usr/local/include/ddccontrol -Wl,--rpath,/usr/local/lib
 *     -lddccontrol -o autorotate autorotate.c
 *
 * You may have to change the libddccontrol installation prefix
 * to suit your system.
 *
 * Usage:
 *
 * autorotate [device]
 *
 * The [device] parameter takes the same arguments as ddccontrol.
 * If omitted, automatic detection of DDC/CI devices is attempted.
 *
 * If everything goes right, the screen rotation will follow the physical
 * rotation of your flat panel until you interrupt the program (CTRL-C).
 */

#include <signal.h>
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>
#include <ddcci.h>
#include <glib.h>
#include <gdk/gdk.h>
#include <gdk/gdkx.h>
#include <X11/extensions/Xrandr.h>

#define ROTATION_CONTROL 0xF8  /* DDC/CI control index */
#define POLL_INTERVAL    1     /* seconds */

static volatile sig_atomic_t termination_requested = FALSE;

static void catch_terminate(G_GNUC_UNUSED int sig)
{
  termination_requested = TRUE;
}

static void setup_signal_handler(int sig, void (* handler) (int))
{
  struct sigaction action;

  sigaction(sig, NULL, &action);

  if (action.sa_handler != SIG_IGN)
  {
    action.sa_handler = handler;
    sigemptyset(&action.sa_mask);
    action.sa_flags = 0;

    sigaction(sig, &action, NULL);
  }
}

static int probe_device(struct monitor* panel, char* device)
{
  int status = FALSE;
  struct monitorlist* list = NULL;

  if (!device)
  {
    struct monitorlist* node;

    list = ddcci_probe();
    node = list;

    while (node && !node->supported)
      node = node->next;

    if (node)
      device = node->filename;
  }

  if (device && ddcci_open(panel, device, FALSE) >= 0)
  {
    unsigned char* name;

    name = (panel->db && panel->db->name) ? panel->db->name : panel->pnpid;

    g_debug("Using device %s: %s", device, (char *) name);

    status = TRUE;
  }

  if (list)
    ddcci_free_list(list);

  return status;
}

static int read_rotation(struct monitor* panel)
{
  unsigned short value   = 0;
  unsigned short maximum = 0;
  int status;

  status = ddcci_readctrl(panel, ROTATION_CONTROL, &value, &maximum);

  if (status <= 0)
  {
    g_debug("ddcci_readctrl() failed with status code %d", status);
    return -1;
  }

  return value;
}

static int update_rotation(GdkScreen* screen, int rotation_index)
{
  GdkWindow* window;
  XRRScreenConfiguration* config;
  Rotation rotations_mask;
  Rotation panel_rotation;
  Rotation screen_rotation = 0;
  int rotation_changed = FALSE;

  window = gdk_screen_get_root_window(screen);

  g_return_val_if_fail(window != NULL, FALSE);

  config = XRRGetScreenInfo(GDK_SCREEN_XDISPLAY(screen), GDK_DRAWABLE_XID(window));

  g_return_val_if_fail(config != NULL, FALSE);

  rotations_mask = XRRConfigRotations(config, &screen_rotation);
  panel_rotation = 1U << rotation_index;

  if ((rotations_mask & panel_rotation) != 0 && (screen_rotation & 0x0F) != panel_rotation)
  {
    Time config_timestamp = 0;
    Time server_time;
    int  screen_size;

    /* Preserve the reflection bits */
    panel_rotation |= screen_rotation & ~0x0FU;

    g_debug("Changing screen rotation from 0x%X to 0x%X",
            (unsigned) screen_rotation, (unsigned) panel_rotation);

    server_time = XRRConfigTimes(config, &config_timestamp);
    screen_size = XRRConfigCurrentConfiguration(config, &screen_rotation);

    if (XRRSetScreenConfig(GDK_SCREEN_XDISPLAY(screen), config, GDK_DRAWABLE_XID(window),
                           screen_size, panel_rotation, server_time) == Success)
    {
      rotation_changed = TRUE;
    }
  }

  XRRFreeScreenConfigInfo(config);

  return rotation_changed;
}

int main(int argc, char** argv)
{
  struct monitor panel;
  GdkScreen* screen;
  int event_basep;
  int error_basep;

  gdk_init(&argc, &argv);

  screen = gdk_screen_get_default();

  g_return_val_if_fail(screen != NULL, 1);

  if (!XRRQueryExtension(GDK_SCREEN_XDISPLAY(screen), &event_basep, &error_basep))
    g_error("XRandR extension not supported on this screen");

  if (!ddcci_init(NULL))
    g_error("Initialization of the DDC/CI library failed");

  if (!probe_device(&panel, (argc >= 2) ? argv[1] : NULL))
  {
    ddcci_release();
    g_error("No DDC/CI capable monitor detected");
  }

  setup_signal_handler(SIGINT,  &catch_terminate);
  setup_signal_handler(SIGHUP,  &catch_terminate);
  setup_signal_handler(SIGTERM, &catch_terminate);

  while (!termination_requested)
  {
    int rotation;

    rotation = read_rotation(&panel);

    if (rotation >= 0 && rotation <= 3)
    {
      /*
       * Accessing the DDC/CI too early after changing the screen
       * rotation triggers "Invalid response" errors on my system.
       */
      if (update_rotation(screen, rotation) && !termination_requested)
        sleep(1);
    }

    if (!termination_requested)
      sleep(POLL_INTERVAL);
  }

  g_debug("Terminated");

  ddcci_close(&panel);
  ddcci_release();

  return 0;
}
