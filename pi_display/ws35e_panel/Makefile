obj-m += ws35e_panel.o

KDIR ?= /usr/src/linux-headers-$(shell uname -r)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

install: all
	sudo cp ws35e_panel.ko /lib/modules/$(shell uname -r)/extra/
	sudo depmod -a
	sudo modprobe ws35e_panel
