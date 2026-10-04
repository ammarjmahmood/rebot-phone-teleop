#!/usr/bin/env bash
set -euo pipefail
KVER="$(uname -r)"
if ! modinfo peak_usb >/dev/null 2>&1; then
  echo "The PEAK USB CAN driver is not in this kernel ($KVER); building it from the matching Linux source"
  BASE="$(echo "$KVER" | grep -oE '^[0-9]+\.[0-9]+')"
  if [ ! -d "/lib/modules/$KVER/build" ]; then
    echo "Kernel headers for $KVER are missing. On Ubuntu run: sudo apt install linux-headers-$KVER"
    exit 1
  fi
  BUILD="$(mktemp -d)"
  SOURCE="https://raw.githubusercontent.com/torvalds/linux/v$BASE"
  for file in pcan_usb.c pcan_usb_core.c pcan_usb_core.h pcan_usb_fd.c pcan_usb_pro.c pcan_usb_pro.h; do
    curl -fsSL -o "$BUILD/$file" "$SOURCE/drivers/net/can/usb/peak_usb/$file"
  done
  mkdir -p "$BUILD/linux/can/dev"
  curl -fsSL -o "$BUILD/linux/can/dev/peak_canfd.h" "$SOURCE/include/linux/can/dev/peak_canfd.h"
  printf 'obj-m += peak_usb.o\npeak_usb-y = pcan_usb_core.o pcan_usb.o pcan_usb_pro.o pcan_usb_fd.o\nccflags-y += -I$(src)\n' > "$BUILD/Kbuild"
  make -s -C "/lib/modules/$KVER/build" M="$BUILD" modules
  sudo install -D -m 644 "$BUILD/peak_usb.ko" "/lib/modules/$KVER/extra/peak_usb.ko"
  sudo depmod -a
fi
sudo modprobe peak_usb
RULE=/etc/udev/rules.d/80-rebot-teleop-can.rules
if [ ! -f "$RULE" ]; then
  echo 'ACTION=="add", SUBSYSTEM=="net", DRIVERS=="peak_usb", RUN+="/usr/sbin/ip link set $name type can bitrate 1000000 restart-ms 100", RUN+="/usr/sbin/ip link set $name up"' | sudo tee "$RULE" >/dev/null
  sudo udevadm control --reload
fi
IFACE=""
for path in /sys/class/net/*; do
  if [ "$(cat "$path/type" 2>/dev/null)" = 280 ] && [ "$(basename "$(readlink -f "$path/device/driver" 2>/dev/null)")" = peak_usb ]; then
    IFACE="$(basename "$path")"
    break
  fi
done
if [ -z "$IFACE" ]; then
  echo "Driver ready, but no PEAK adapter is plugged in. Plug it in; it is configured automatically from now on."
  exit 0
fi
sudo ip link set "$IFACE" down
sudo ip link set "$IFACE" type can bitrate 1000000 restart-ms 100
sudo ip link set "$IFACE" up
echo "CAN ready on $IFACE at 1 Mbit/s"
