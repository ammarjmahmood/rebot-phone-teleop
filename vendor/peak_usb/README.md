# PEAK USB CAN driver

Source of the `peak_usb` kernel module used by the PEAK USB CAN adapters on both arms. The built module is installed at `/lib/modules/<kernel>/extra/peak_usb.ko`. After a kernel update, rebuild and install it:

```bash
cd vendor/peak_usb
make -C /lib/modules/$(uname -r)/build M=$PWD modules
sudo install -m 644 peak_usb.ko /lib/modules/$(uname -r)/extra/
sudo depmod -a && sudo modprobe peak_usb
```
