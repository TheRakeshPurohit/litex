#
# This file is part of LiteX.
#
# Copyright (c) 2022 Ilia Sergachev <ilia@sergachev.ch>
# SPDX-License-Identifier: BSD-2-Clause

import os
from migen import *
from litex.soc.interconnect import wishbone
from litex.soc.cores.cpu import CPU
from litex.soc.interconnect import axi


class CortexM1(CPU):
    variants             = ["standard"]
    family               = "arm"
    name                 = "cortex_m1"
    human_name           = "ARM Cortex-M1"
    data_width           = 32
    endianness           = "little"
    reset_address        = 0x0000_0000
    gcc_triple           = "arm-none-eabi"
    gcc_flags            = "-march=armv6-m -mthumb -mfloat-abi=soft"
    linker_output_format = "elf32-littlearm"
    nop                  = "nop"
    io_regions           = {0x4000_0000: 0x2000_0000,
                            0xA000_0000: 0x6000_0000
                            }  # Origin, Length.

    @property
    def mem_map(self):
        return {
            "rom"      : 0x0000_0000,
            "sram"     : 0x2000_0000,
            "main_ram" : 0x1000_0000,
            "csr"      : 0xA000_0000
        }

    def __init__(self, platform, variant, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.platform       = platform
        self.reset          = Signal()
        self.interrupt      = Signal(2)
        pbus                = wishbone.Interface(data_width=32, adr_width=30)
        self.periph_buses   = [pbus]
        self.memory_buses   = []

        def _mem_size(x):
            return log2_int(x // 512)

        self.cpu_params = dict(
            i_HCLK      = ClockSignal("sys"),
            i_SYSRESETn = ~(ResetSignal() | self.reset),
            p_NUM_IRQ   = len(self.interrupt),
            i_IRQ       = self.interrupt,
            i_DBGRESETn = ~(ResetSignal() | self.reset),
            p_ITCM_SIZE = _mem_size(0 * 1024),  # embedded ROM
            p_DTCM_SIZE = _mem_size(0 * 1024),  # embedded RAM
            p_DEBUG_SEL = 1,  # JTAG
            p_SMALL_DEBUG = True,
            i_CFGITCMEN = 0,  # 1 = alias ITCM at 0x0
            o_DBGRESTARTED = Signal(),
            o_LOCKUP = Signal(),
            o_HALTED = Signal(),
            o_SYSRESETREQ = Signal(),
            i_NMI = 0,
            i_EDBGRQ = 0,
            i_DBGRESTART = 0,
        )

        def connect_axi(axi_bus, suffix=''):
            layout = axi_bus.layout_flat()
            dir_map = {DIR_M_TO_S: 'o', DIR_S_TO_M: 'i'}
            for group, signal, direction in layout:
                if signal in ['id', 'qos', 'first']:
                    continue
                if signal == 'last':
                    if group in ['b', 'a', 'ar', 'aw']:
                        continue
                prefix = 'H' if signal == 'data' else ''
                direction = dir_map[direction]
                self.cpu_params[f'{direction}_{prefix}{group.upper()}{signal.upper()}{suffix}'] = \
                    getattr(getattr(axi_bus, group), signal)

        pbus_axi = axi.AXIInterface(data_width=self.data_width, address_width=32)
        pbus_a2w = axi.AXI2Wishbone(pbus_axi, pbus, base_address=0)
        self.submodules += pbus_a2w
        connect_axi(pbus_axi)

        platform.add_source_dir("AT472-BU-98000-r0p1-00rel0/vivado/Arm_ipi_repository/CM1DbgAXI/logical/rtl")

    def connect_jtag(self, pads):
        self.cpu_params.update(
            i_SWDITMS  = pads.tms,
            i_TDI      = pads.tdi,
            o_TDO      = pads.tdo,
            o_nTDOEN   = Signal(),
            i_nTRST    = pads.ntrst,
            i_SWCLKTCK = pads.tck,
            o_JTAGNSW  = Signal(),  # Indicates debug mode, JTAG or SWD
            o_JTAGTOP  = Signal(),  # ?
            o_SWDO     = Signal(),  # TODO
            o_SWDOEN   = Signal(),  # TODO
        )

    def do_finalize(self):
        self.specials += Instance("CortexM1DbgAXI", **self.cpu_params)