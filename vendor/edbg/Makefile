COMPILER ?= gcc
UNAME ?= $(shell uname)

SRCS = \
  dap.c \
  edbg.c \
  utils.c \
  target.c \
  target_atmel_cm0p.c \
  target_atmel_cm3.c \
  target_atmel_cm4.c \
  target_atmel_cm7.c \
  target_atmel_cm4v2.c \
  target_mchp_cm23.c \
  target_st_stm32g0.c \
  target_st_stm32g4.c \
  target_st_stm32wb55.c \
  target_gd_gd32f4xx.c \
  target_nu_m480.c \
  target_lattice_lcmxo2.c \
  target_rpi_rp2040.c \
  target_puya_py32f0.c \

HDRS = \
  dap.h \
  dbg.h \
  edbg.h \
  utils.h \
  target.h

ifeq ($(UNAME), Linux)
  BIN = edbg
  SRCS += dbg_lin.c
  LIBS += -ludev
else
  ifeq ($(UNAME), Darwin)
    BIN = edbg
    SRCS += dbg_mac.c
    LIBS += -framework IOKit
    LIBS += -framework Foundation
    LIBS += -framework CoreFoundation
    LIBS += -framework Cocoa
  else
    BIN = edbg.exe
    SRCS += dbg_win.c
    LIBS += -lhid -lwinusb -lsetupapi
  endif
endif

CFLAGS += -W -Wall -Wextra -O3 -std=gnu11
#CFLAGS += -fno-diagnostics-show-caret

all: $(BIN)

$(BIN): $(SRCS) $(HDRS)
	$(COMPILER) $(CFLAGS) $(SRCS) $(LIBS) -o $(BIN)

# mini-A-bootload-tool: same source, built with the unique-serial-number
# read path enabled (see EDBG_DIAG_SERIAL in target.c / target_atmel_cm0p.c).
# Kept as a separate binary so the plain $(BIN) used for flashing never has
# its bounds checks altered.
edbg-diag: $(SRCS) $(HDRS)
	$(COMPILER) $(CFLAGS) -DEDBG_DIAG_SERIAL $(SRCS) $(LIBS) -o edbg-diag

clean:
	rm -rvf $(BIN) edbg-diag

