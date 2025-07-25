#
# This file is part of LiteX.
#
# This file is Copyright (c) 2014-2022 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2013-2014 Sebastien Bourdeauducq <sb@m-labs.hk>
# This file is Copyright (c) 2019 Gabriel L. Somlo <somlo@cmu.edu>
# SPDX-License-Identifier: BSD-2-Clause

import os
import sys
import math
import time
import logging
import argparse
import datetime

from migen import *

from litex.gen                import colorer
from litex.gen                import LiteXModule, LiteXContext
from litex.gen.genlib.misc    import WaitTimer
from litex.gen.fhdl.hierarchy import LiteXHierarchyExplorer

from litex.compat.soc_core import *

from litex.soc.interconnect.csr              import *
from litex.soc.interconnect.csr_eventmanager import *
from litex.soc.interconnect                  import csr_bus
from litex.soc.interconnect                  import stream
from litex.soc.interconnect                  import wishbone
from litex.soc.interconnect                  import axi
from litex.soc.interconnect                  import ahb


# Helpers ------------------------------------------------------------------------------------------

def auto_int(x):
    return int(x, 0)

def build_time(with_time=True):
    fmt = "%Y-%m-%d %H:%M:%S" if with_time else "%Y-%m-%d"
    return datetime.datetime.fromtimestamp(time.time()).strftime(fmt)

def add_ip_address_constants(soc, name, ip_address, check_duplicate=True):
    _ip_address = ip_address.split(".")
    assert len(_ip_address) == 4
    for n in range(4):
        assert int(_ip_address[n]) < 256
        soc.add_constant(f"{name}{n+1}", int(_ip_address[n]), check_duplicate=check_duplicate)

def add_mac_address_constants(soc, name, mac_address, check_duplicate=True):
    assert mac_address < 2**48
    for n in range(6):
        soc.add_constant(f"{name}{n+1}", (mac_address >> ((5 - n) * 8)) & 0xff, check_duplicate=check_duplicate)

# SoCError -----------------------------------------------------------------------------------------

class SoCError(Exception):
    def __init__(self):
        sys.stderr = None # Error already described, avoid traceback/exception.

# SoCConstant --------------------------------------------------------------------------------------

def SoCConstant(value):
    return value

# SoCRegion ----------------------------------------------------------------------------------------

class SoCRegion:
    def __init__(self, origin=None, size=None, mode="rw", cached=True, linker=False, decode=True):
        self.logger    = logging.getLogger("SoCRegion")
        self.origin    = origin
        self.decode    = decode
        self.size      = size
        if size != 2**log2_int(size, False):
            self.logger.info("Region size {} internally from {} to {}.".format(
                colorer("rounded", color="cyan"),
                colorer("0x{:08x}".format(size)),
                colorer("0x{:08x}".format(2**log2_int(size, False)))))
        self.size_pow2 = 2**log2_int(size, False)
        self.mode      = mode
        self.cached    = cached
        self.linker    = linker
        self.type      = ""

    def decoder(self, bus):
        origin = self.origin
        size   = self.size_pow2
        if (origin & (size - 1)) != 0:
            self.logger.error("Origin needs to be aligned on size:")
            self.logger.error(self)
            raise SoCError()
        if (not self.decode) or ((origin == 0) and (size == 2**bus.address_width)):
            return lambda a: True
        origin >>= int(math.log2(bus.data_width//8)) # bytes to words aligned.
        size   >>= int(math.log2(bus.data_width//8)) # bytes to words aligned.
        return lambda a: (a[log2_int(size):] == (origin >> log2_int(size)))

    def __str__(self):
        r = ""
        if self.origin is not None:
            r += "Origin: {:>16}, ".format(colorer("0x{:08x}".format(self.origin)))
        if self.size is not None:
            r += "Size: {:>16}, ".format(colorer("0x{:08x}".format(self.size)))
        r += "Mode: {:>11}, ".format(colorer(self.mode.upper()))
        r += "Cached: {:>13}, ".format(colorer(self.cached))
        r += "Linker: {:>13}".format(colorer(self.linker))
        return r

class SoCIORegion(SoCRegion): pass

# SoCCSRRegion -------------------------------------------------------------------------------------

class SoCCSRRegion:
    def __init__(self, origin, busword, obj):
        self.origin  = origin
        self.busword = busword
        self.obj     = obj

# SoCBusHandler ------------------------------------------------------------------------------------

class SoCBusHandler(LiteXModule):
    supported_standard      = ["wishbone", "axi-lite", "axi"]
    supported_data_width    = [32, 64, 128, 256, 512]
    supported_address_width = [32, 64]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, name="SoCBusHandler",
        standard         = "wishbone",
        data_width       = 32,
        address_width    = 32,
        timeout          = 1e6,
        bursting         = False,
        interconnect     = "shared", interconnect_register=True,
        reserved_regions = {}
    ):
        self.logger = logging.getLogger(name)
        self.logger.info("Creating Bus Handler...")

        # Check Bus Standard.
        if standard not in self.supported_standard:
            self.logger.error("Unsupported {} {}, supported are: {:s}".format(
                colorer("Bus standard", color="red"),
                colorer(standard),
                colorer(", ".join(self.supported_standard))))
            raise SoCError()

        # Check Bus Data Width.
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported {} {}, supported are: {:s}".format(
                colorer("Data Width", color="red"),
                colorer(data_width),
                colorer(", ".join(str(x) for x in self.supported_data_width))))
            raise SoCError()

        # Check Bus Address Width.
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported {} {}, supported are: {:s}".format(
                colorer("Address Width", color="red"),
                colorer(address_width),
                colorer(", ".join(str(x) for x in self.supported_address_width))))
            raise SoCError()

        # Create Bus
        self.standard              = standard
        self.data_width            = data_width
        self.address_width         = address_width
        self.addressing            = {
            "wishbone" : "word", # FIXME: Allow selection for Wishbone.
            "axi-lite" : "byte",
            "axi"      : "byte",
        }[standard]
        self.bursting              = bursting
        self.interconnect          = interconnect
        self.interconnect_register = interconnect_register
        self.masters               = {}
        self.slaves                = {}
        self.regions               = {}
        self.io_regions            = {}
        self.io_regions_check      = True
        self.timeout               = timeout
        self.logger.info("{}-bit {} Bus, {}GiB Address Space.".format(
            colorer(data_width), colorer(standard), colorer(2**address_width/2**30)))

        # Add reserved regions.
        self.logger.info("Adding {} Bus Regions...".format(colorer("reserved", color="cyan")))
        for name, region in reserved_regions.items():
            if isinstance(region, int):
                region = SoCRegion(origin=region, size=0x1000000)
            self.add_region(name, region)

        self.logger.info("Bus Handler {}.".format(colorer("created", color="green")))

    # Add/Alloc/Check Regions ----------------------------------------------------------------------
    def add_region(self, name, region):
        allocated = False
        if name in self.regions.keys() or name in self.io_regions.keys():
            self.logger.error("{} already declared as Region:".format(colorer(name, color="red")))
            self.logger.error(self)
            raise SoCError()
        # Check if is SoCIORegion.
        if isinstance(region, SoCIORegion):
            self.io_regions[name] = region
            # Check for overlap with others IO regions.
            overlap = self.check_regions_overlap(self.io_regions)
            if overlap is not None:
                self.logger.error("IO Region {} between {} and {}:".format(
                    colorer("overlap", color="red"),
                    colorer(overlap[0]),
                    colorer(overlap[1])))
                self.logger.error(str(self.io_regions[overlap[0]]))
                self.logger.error(str(self.io_regions[overlap[1]]))
                raise SoCError()
            self.logger.info("{} Region {} at {}.".format(
                colorer(name,    color="underline"),
                colorer("added", color="green"),
                str(region)))
        # Check if is SoCRegion
        elif isinstance(region, SoCRegion):
            # If no Origin specified, allocate Region.
            if region.origin is None:
                allocated = True
                region    = self.alloc_region(name, region.size, region.cached)
                self.regions[name] = region
            # Else add Region.
            else:
                if self.io_regions_check:
                    if self.check_region_is_io(region):
                        # If Region is an IO Region it is not cached.
                        if region.cached:
                            self.logger.error("{} {}".format(
                                colorer(name + " Region in IO region, it can't be cached:", color="red"),
                                str(region)))
                            self.logger.error(self)
                            raise SoCError()
                    else:
                        # If Region is not an IO Region it is cached.
                        if not region.cached:
                            self.logger.error("{} {}".format(
                                colorer(name + " Region not in IO region, it must be cached:", color="red"),
                                str(region)))
                            self.logger.error(self)
                            raise SoCError()
                self.regions[name] = region
                # Check for overlap with others IO regions.
                overlap = self.check_regions_overlap(self.regions)
                if overlap is not None:
                    self.logger.error("Region {} between {} and {}:".format(
                        colorer("overlap", color="red"),
                        colorer(overlap[0]),
                        colorer(overlap[1])))
                    self.logger.error(str(self.regions[overlap[0]]))
                    self.logger.error(str(self.regions[overlap[1]]))
                    raise SoCError()
            self.logger.info("{} Region {} at {}.".format(
                colorer(name, color="underline"),
                colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
                str(region)))
        else:
            self.logger.error("{} is not a supported Region.".format(colorer(name, color="red")))
            raise SoCError()

    def alloc_region(self, name, size, cached=True):
        self.logger.info("Allocating {} Region of size {}...".format(
            colorer("Cached" if cached else "IO"),
            colorer("0x{:08x}".format(size))))

        # Limit Search Regions.
        if cached == False:
            search_regions = self.io_regions
        else:
            search_regions = {"main": SoCRegion(origin=0x00000000, size=2**self.address_width-1)}

        # Iterate on Search_Regions to find a Candidate.
        size_pow2 = 2**log2_int(size, False)
        for _, search_region in search_regions.items():
            origin = search_region.origin
            while (origin + size) < (search_region.origin + search_region.size_pow2):
                # Align Origin on Size.
                if (origin%size_pow2):
                    origin += (size_pow2 - origin%size_pow2)
                    continue
                # Create a Candidate.
                candidate = SoCRegion(origin=origin, size=size, cached=cached)
                overlap   = False
                # Check Candidate does not overlap with allocated existing regions.
                for _, allocated in self.regions.items():
                    if self.check_regions_overlap({"0": allocated, "1": candidate}) is not None:
                        origin += size
                        overlap = True
                        break
                if not overlap:
                    # If no overlap, the Candidate is selected.
                    return candidate

        self.logger.error("Not enough Address Space to allocate Region.")
        raise SoCError()

    def check_regions_overlap(self, regions, check_linker=False):
        i = 0
        while i < len(regions):
            n0 =  list(regions.keys())[i]
            r0 = regions[n0]
            for n1 in list(regions.keys())[i+1:]:
                r1 = regions[n1]
                if r0.linker or r1.linker:
                    if not check_linker:
                        continue
                if r0.origin >= (r1.origin + r1.size_pow2):
                    continue
                if r1.origin >= (r0.origin + r0.size_pow2):
                    continue
                return (n0, n1)
            i += 1
        return None

    def check_region_is_in(self, region, container):
        is_in = True
        if not (region.origin >= container.origin):
            is_in = False
        if not ((region.origin + region.size) <= (container.origin + container.size)):
            is_in = False
        return is_in

    def check_region_is_io(self, region):
        is_io = False
        for _, io_region in self.io_regions.items():
            if self.check_region_is_in(region, io_region):
                is_io = True
        return is_io

    # Add Master/Slave -----------------------------------------------------------------------------
    def add_adapter(self, name, interface, direction="m2s"):
        assert direction in ["m2s", "s2m"]

        # Bus-Data-Width conversion helper.
        def bus_data_width_convert(interface, direction):
            # Same Data-Width, return un-modified interface.
            if interface.data_width == self.data_width:
                return interface
            # Different Data-Width: Return adapted interface.
            else:
                interface_cls = type(interface)
                converter_cls = {
                    wishbone.Interface   : wishbone.Converter,
                    axi.AXILiteInterface : axi.AXILiteConverter,
                    axi.AXIInterface     : axi.AXIConverter,
                }[interface_cls]
                args = {
                    "data_width"    : self.data_width,
                    "address_width" : self.address_width,
                    "addressing"    : interface.addressing,
                    "bursting"      : interface.bursting,
                }
                if isinstance(interface, axi.AXIInterface):
                    args.update({
                        "version"       : interface.version,
                        "id_width"      : interface.id_width,
                        "aw_user_width" : interface.aw.user_width,
                        "w_user_width"  : interface.w.user_width,
                        "b_user_width"  : interface.b.user_width,
                        "ar_user_width" : interface.ar.user_width,
                        "r_user_width"  : interface.r.user_width,
                    })
                adapted_interface = interface_cls(**args)

                if direction == "m2s":
                    master, slave = interface, adapted_interface
                elif direction == "s2m":
                    master, slave = adapted_interface, interface
                converter = converter_cls(master=master, slave=slave)
                self.submodules += converter
                return adapted_interface

        # Bus-Addressing conversion helper.
        def bus_addressing_convert(interface, direction):
            # Same Addressing, return un-modified interface.
            if interface.addressing == self.addressing:
                return interface
            # AXI/AXI-Lite/AHB interface, Bus-Addressing conversion already handled in Bus-Standard conversion.
            elif isinstance(interface, (axi.AXIInterface, axi.AXILiteInterface, ahb.AHBInterface)):
                return interface
            # Different Addressing: Return adapted interface.
            else:
                interface_cls = type(interface)
                adapted_interface = interface_cls(
                    data_width    = self.data_width,
                    address_width = self.address_width,
                    addressing    = self.addressing,
                )
                address_shift = log2_int(interface.data_width//8)
                if direction == "m2s":
                    self.comb += interface.connect(adapted_interface, omit={"adr"})
                    if (interface.addressing == "word") and (self.addressing == "byte"):
                        self.comb += adapted_interface.adr[address_shift:].eq(interface.adr)
                    if (interface.addressing == "byte") and (self.addressing == "word"):
                        self.comb += adapted_interface.adr.eq(interface.adr[address_shift:])
                if direction == "s2m":
                    self.comb += adapted_interface.connect(interface, omit={"adr"})
                    if (interface.addressing == "word") and (self.addressing == "byte"):
                        self.comb += interface.adr.eq(adapted_interface.adr[address_shift:])
                    if (interface.addressing == "byte") and (self.addressing == "word"):
                        self.comb += interface.adr[address_shift:].eq(adapted_interface.adr)
                return adapted_interface

        # Bus-Standard conversion helper.
        def bus_standard_convert(interface, direction):
            main_bus_cls = {
                "wishbone": wishbone.Interface,
                "axi-lite": axi.AXILiteInterface,
                "axi"     : axi.AXIInterface,
            }[self.standard]
            # Same Bus-Standard: Return un-modified interface.
            if isinstance(interface, main_bus_cls):
                return interface
            # Different Bus-Standard: Return adapted interface.
            else:
                adapted_interface = main_bus_cls(
                    data_width    = self.data_width,
                    address_width = self.address_width,
                    addressing    = self.addressing,
                )
                if direction == "m2s":
                    master, slave = interface, adapted_interface
                elif direction == "s2m":
                    master, slave = adapted_interface, interface
                bridge_cls = {
                    # Bus from           , Bus to               , Bridge
                    (wishbone.Interface  , axi.AXILiteInterface): axi.Wishbone2AXILite,
                    (axi.AXILiteInterface, wishbone.Interface)  : axi.AXILite2Wishbone,
                    (wishbone.Interface  , axi.AXIInterface)    : axi.Wishbone2AXI,
                    (axi.AXILiteInterface, axi.AXIInterface)    : axi.AXILite2AXI,
                    (axi.AXIInterface,     axi.AXILiteInterface): axi.AXI2AXILite,
                    (axi.AXIInterface,     wishbone.Interface)  : axi.AXI2Wishbone,
                    (ahb.AHBInterface,     wishbone.Interface)  : ahb.AHB2Wishbone,
                }[type(master), type(slave)]
                bridge = bridge_cls(master, slave)
                self.submodules += bridge
                return adapted_interface

        # Interface conversion.
        adapted_interface = interface
        adapted_interface = bus_data_width_convert(adapted_interface, direction)
        adapted_interface = bus_addressing_convert(adapted_interface, direction)
        adapted_interface =   bus_standard_convert(adapted_interface, direction)

        if type(interface) != type(adapted_interface) or interface.data_width != adapted_interface.data_width:
            fmt = "{name} Bus {adapted} from {from_bus} {from_bits}-bit to {to_bus} {to_bits}-bit."
            bus_names = {
                wishbone.Interface:   "Wishbone",
                axi.AXILiteInterface: "AXI-Lite",
                axi.AXIInterface:     "AXI",
                ahb.AHBInterface:     "AHB",
            }
            self.logger.info(fmt.format(
                name      = colorer(name),
                adapted   = colorer("adapted", color="cyan"),
                from_bus  = colorer(bus_names[type(interface)]),
                from_bits = colorer(interface.data_width),
                to_bus    = colorer(bus_names[type(adapted_interface)]),
                to_bits   = colorer(adapted_interface.data_width)))

        return adapted_interface

    # Add Remapper ---------------------------------------------------------------------------------
    def add_remapper(self, name, interface, origin, size):
        interface_cls = type(interface)
        remapper_cls  = {
            wishbone.Interface   : wishbone.Remapper,
            axi.AXILiteInterface : axi.AXILiteRemapper,
            axi.AXIInterface     : axi.AXIRemapper,
        }[interface_cls]

        adapted_interface = interface_cls(
            data_width    = interface.data_width,
            address_width = interface.address_width,
            addressing    = interface.addressing,
        )

        self.submodules += remapper_cls(interface, adapted_interface, origin, size)

        fmt = "{name} Bus {remapped} to {origin} (Size: {size})."
        self.logger.info(fmt.format(
            name     = colorer(name),
            remapped = colorer("remapped", color="cyan"),
            origin   = colorer(f"0x{origin:08x}"),
            size     = colorer(f"0x{size:08x}"),
        ))

        return adapted_interface
    
    # Add Offset ---------------------------------------------------------------------------------
    def add_offset(self, name, interface, offset):
        interface_cls = type(interface)
        offset_cls  = {
            wishbone.Interface   : wishbone.Offset,
            axi.AXILiteInterface : axi.AXILiteOffset,
            axi.AXIInterface     : axi.AXIOffset,
        }[interface_cls]

        adapted_interface = interface_cls(
            data_width    = interface.data_width,
            address_width = interface.address_width,
            addressing    = interface.addressing,
        )

        self.submodules += offset_cls(adapted_interface, interface, offset)

        fmt = "{name} Bus {offseted} by {offset}."
        self.logger.info(fmt.format(
            name     = colorer(name),
            offseted = colorer("offseted", color="cyan"),
            offset   = colorer(f"0x{offset:08x}"),
        ))

        return adapted_interface

    def add_master(self, name=None, master=None, region=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} {} as Bus Master:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        if region:
            master = self.add_remapper(name, master, region.origin, region.size)
        master = self.add_adapter(name, master, "m2s")
        self.masters[name] = master
        self.logger.info("{} {} as Bus Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    def add_controller(self, name=None, controller=None):
        self.add_master(name=name, master=controller)

    def add_slave(self, name=None, slave=None, region=None, strip_origin=False):
        no_name   = name   is None
        no_region = region is None
        if no_name and no_region:
            self.logger.error("Please {} {} or/and {} of Bus Slave.".format(
                colorer("specify", color="red"),
                colorer("name"),
                colorer("region")))
            raise SoCError()
        if no_name:
            name = "slave{:d}".format(len(self.slaves))
        if no_region:
            region = self.regions.get(name, None)
            if region is None:
                self.logger.error("{} Region {}.".format(
                    colorer(name),
                    colorer("not found", color="red")))
                raise SoCError()
        else:
             self.add_region(name, region)
        if name in self.slaves.keys():
            self.logger.error("{} {} as Bus Slave:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        if strip_origin:
            slave = self.add_offset(name, slave, self.regions[name].origin)
        slave = self.add_adapter(name, slave, "s2m")
        self.slaves[name] = slave
        self.logger.info("{} {} as Bus Slave.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))

    def add_peripheral(self, name=None, peripheral=None, region=None):
        self.add_slave(name=name, slave=peripheral, region=region)

    def get_address_width(self, standard):
        standard_from = self.standard
        standard_to   = standard

        # AXI or AXI-Lite SoC Bus and Wishbone requested:
        if standard_from in ["axi", "axi-lite"] and standard_to in ["wishbone"]:
            address_shift = log2_int(self.data_width//8)
            return self.address_width - address_shift
        # Wishbone SoC Bus and AXI, AXI-Lite requested:
        if standard_from in ["wishbone"] and standard_to in ["axi", "axi-lite"]:
            address_shift = log2_int(self.data_width//8)
            return self.address_width + address_shift
        # Else just return address_width:
        return self.address_width

    def do_finalize(self):
        interconnect_p2p_cls = {
            "wishbone": wishbone.InterconnectPointToPoint,
            "axi-lite": axi.AXILiteInterconnectPointToPoint,
            "axi"     : axi.AXIInterconnectPointToPoint,
        }[self.standard]
        interconnect_shared_cls = {
            "wishbone": wishbone.InterconnectShared,
            "axi-lite": axi.AXILiteInterconnectShared,
            "axi"     : axi.AXIInterconnectShared,
        }[self.standard]
        interconnect_crossbar_cls = {
            "wishbone": wishbone.Crossbar,
            "axi-lite": axi.AXILiteCrossbar,
            "axi"     : axi.AXICrossbar,
        }[self.standard]

        self._interconnect = None
        if len(self.masters) and len(self.slaves):
            # If 1 bus_master, 1 bus_slave and no address translation, use InterconnectPointToPoint.
            if ((len(self.masters) == 1)  and
                (len(self.slaves)  == 1)  and
                (next(iter(self.regions.values())).origin == 0)):
                self._interconnect = interconnect_p2p_cls(
                    master = next(iter(self.masters.values())),
                    slave  = next(iter(self.slaves.values())))
            # Otherwise, use InterconnectShared/Crossbar.
            else:
                # Check Region decoder use.
                if len(self.regions) > 1:
                    for region in self.regions.values():
                        if region.decode == False:
                            self.logger.error("Only {} Region can be used when {} Decoder.".format(
                                colorer("one",       color="red"),
                                colorer("disabling", color="red"),
                            ))
                            self.logger.error(self)
                            raise SoCError()
                # Interconnect Logic.
                interconnect_cls = {
                    "shared"  : interconnect_shared_cls,
                    "crossbar": interconnect_crossbar_cls,
                }[self.interconnect]
                self._interconnect = interconnect_cls(
                    masters        = list(self.masters.values()),
                    slaves         = [(self.regions[n].decoder(self), s) for n, s in self.slaves.items()],
                    register       = self.interconnect_register,
                    timeout_cycles = self.timeout
                )
            self.logger.info("Interconnect: {} ({} <-> {}).".format(
                colorer(self._interconnect.__class__.__name__),
                colorer(len(self.masters)),
                colorer(len(self.slaves))))

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit {} Bus, {}GiB Address Space.\n".format(
            colorer(self.data_width), colorer(self.standard), colorer(2**self.address_width/2**30))
        r += "IO Regions: ({})\n".format(len(self.io_regions.keys())) if len(self.io_regions.keys()) else ""
        io_regions = {k: v for k, v in sorted(self.io_regions.items(), key=lambda item: item[1].origin)}
        for name, region in io_regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Regions: ({})\n".format(len(self.regions.keys())) if len(self.regions.keys()) else ""
        regions = {k: v for k, v in sorted(self.regions.items(), key=lambda item: item[1].origin)}
        for name, region in regions.items():
           r += colorer(name, color="underline") + " "*(20-len(name)) + ": " + str(region) + "\n"
        r += "Bus Masters: ({})\n".format(len(self.masters.keys())) if len(self.masters.keys()) else ""
        for name in self.masters.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r += "Bus Slaves: ({})\n".format(len(self.slaves.keys())) if len(self.slaves.keys()) else ""
        for name in self.slaves.keys():
           r += "- {}\n".format(colorer(name, color="underline"))
        r = r[:-1]
        return r

# SoCLocHandler ------------------------------------------------------------------------------------

class SoCLocHandler(LiteXModule):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, name, n_locs):
        self.name   = name
        self.locs   = {}
        self.n_locs = n_locs

    # Add ------------------------------------------------------------------------------------------
    def add(self, name, n=None, use_loc_if_exists=False):
        allocated = False
        if not (use_loc_if_exists and name in self.locs.keys()):
            if name in self.locs.keys():
                self.logger.error("{} {} name {}.".format(
                    colorer(name), self.name, colorer("already used", color="red")))
                self.logger.error(self)
                raise SoCError()
            if n in self.locs.values():
                self.logger.error("{} {} Location {}.".format(
                    colorer(n), self.name, colorer("already used", color="red")))
                self.logger.error(self)
                raise SoCError()
            if n is None:
                allocated = True
                n = self.alloc(name)
            else:
                if n < 0:
                    self.logger.error("{} {} Location should be {}.".format(
                        colorer(n),
                        self.name,
                        colorer("positive", color="red")))
                    raise SoCError()
                if n > self.n_locs:
                    self.logger.error("{} {} Location {} than maximum: {}.".format(
                        colorer(n),
                        self.name,
                        colorer("higher", color="red"),
                        colorer(self.n_locs)))
                    raise SoCError()
            self.locs[name] = n
        else:
            n = self.locs[name]
        self.logger.info("{} {} {} at Location {}.".format(
            colorer(name, color="underline"),
            self.name,
            colorer("allocated" if allocated else "added", color="cyan" if allocated else "green"),
            colorer(n)))

    # Alloc ----------------------------------------------------------------------------------------
    def alloc(self, name):
        for n in range(self.n_locs):
            if n not in self.locs.values():
                return n
        self.logger.error("Not enough Locations.")
        self.logger.error(self)
        raise SoCError()

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{} Locations: ({})\n".format(self.name, len(self.locs.keys())) if len(self.locs.keys()) else ""
        locs = {k: v for k, v in sorted(self.locs.items(), key=lambda item: item[1])}
        length = 0
        for name in locs.keys():
            if len(name) > length: length = len(name)
        for name in locs.keys():
           r += "- {}{}: {}\n".format(colorer(name, color="underline"), " "*(length + 1 - len(name)), colorer(self.locs[name]))
        return r

# SoCCSRHandler ------------------------------------------------------------------------------------

class SoCCSRHandler(SoCLocHandler):
    supported_data_width    = [8, 32]
    supported_address_width = [14, 15, 16, 17, 18]
    supported_alignment     = [32]
    supported_paging        = [0x400, 0x800, 0x1000, 0x2000, 0x4000]
    supported_ordering      = ["big", "little"]

    # Creation -------------------------------------------------------------------------------------
    def __init__(self, data_width=32, address_width=14, alignment=32, paging=0x800, ordering="big", reserved_csrs={}):
        SoCLocHandler.__init__(self, "CSR", n_locs=alignment//8*(2**address_width)//paging)
        self.logger = logging.getLogger("SoCCSRHandler")
        self.logger.info("Creating CSR Handler...")

        # Check CSR Data Width.
        if data_width not in self.supported_data_width:
            self.logger.error("Unsupported {} {}, supported are: {:s}".format(
                colorer("Data Width", color="red"),
                colorer(data_width),
                colorer(", ".join(str(x) for x in self.supported_data_width))))
            raise SoCError()

        # Check CSR Address Width.
        if address_width not in self.supported_address_width:
            self.logger.error("Unsupported {} {} supported are: {:s}".format(
                colorer("Address Width", color="red"),
                colorer(address_width),
                colorer(", ".join(str(x) for x in self.supported_address_width))))
            raise SoCError()

        # Check CSR Alignment.
        if alignment not in self.supported_alignment:
            self.logger.error("Unsupported {}: {} supported are: {:s}".format(
                colorer("Alignment", color="red"),
                colorer(alignment),
                colorer(", ".join(str(x) for x in self.supported_alignment))))
            raise SoCError()
        if data_width > alignment:
            self.logger.error("Alignment ({}) {} Data Width ({})".format(
                colorer(alignment),
                colorer("should be >=", color="red"),
                colorer(data_width)))
            raise SoCError()

        # Check CSR Paging.
        if paging not in self.supported_paging:
            self.logger.error("Unsupported {} 0x{}, supported are: {:s}".format(
                colorer("Paging", color="red"),
                colorer("{:x}".format(paging)),
                colorer(", ".join("0x{:x}".format(x) for x in self.supported_paging))))
            raise SoCError()

        # Check CSR Ordering.
        if ordering not in self.supported_ordering:
            self.logger.error("Unsupported {} {}, supported are: {:s}".format(
                colorer("Ordering", color="red"),
                colorer("{}".format(paging)),
                colorer(", ".join("{}".format(x) for x in self.supported_ordering))))
            raise SoCError()

        # Create CSR Handler.
        self.data_width    = data_width
        self.address_width = address_width
        self.alignment     = alignment
        self.paging        = paging
        self.ordering      = ordering
        self.masters       = {}
        self.regions       = {}
        self.logger.info("{}-bit CSR Bus, {}-bit Aligned, {}KiB Address Space, {}B Paging, {} Ordering (Up to {} Locations).".format(
            colorer(self.data_width),
            colorer(self.alignment),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.ordering),
            colorer(self.n_locs)))

        # Add reserved CSRs.
        self.logger.info("Adding {} CSRs...".format(colorer("reserved", color="cyan")))
        for name, n in reserved_csrs.items():
            self.add(name, n)

        self.logger.info("CSR Handler {}.".format(colorer("created", color="green")))

    # Add Master -----------------------------------------------------------------------------------
    def add_master(self, name=None, master=None):
        if name is None:
            name = "master{:d}".format(len(self.masters))
        if name in self.masters.keys():
            self.logger.error("{} {} as CSR Master:".format(
                colorer(name),
                colorer("already declared", color="red")))
            self.logger.error(self)
            raise SoCError()
        if master.data_width != self.data_width:
            self.logger.error("{} Master/Handler Data Width {} ({} vs {}).".format(
                colorer(name),
                colorer("missmatch", color="red"),
                colorer(master.data_width),
                colorer(self.data_width)))
            raise SoCError()
        self.masters[name] = master
        self.logger.info("{} {} as CSR Master.".format(
            colorer(name,    color="underline"),
            colorer("added", color="green")))

    # Add Region -----------------------------------------------------------------------------------
    def add_region(self, name, region):
        # FIXME: add checks
        self.regions[name] = region

    # Address map ----------------------------------------------------------------------------------
    def address_map(self, name, memory):
        if memory is not None:
            name = name + "_" + memory.name_override
        if self.locs.get(name, None) is None:
            self.add(name, use_loc_if_exists=True)
        return self.locs[name]

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r = "{}-bit CSR Bus, {}-bit Aligned, {}KiB Address Space, {}B Paging, {} Ordering (Up to {} Locations).\n".format(
            colorer(self.data_width),
            colorer(self.alignment),
            colorer(2**self.address_width/2**10),
            colorer(self.paging),
            colorer(self.ordering),
            colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCIRQHandler ------------------------------------------------------------------------------------

class SoCIRQHandler(SoCLocHandler):
    # Creation -------------------------------------------------------------------------------------
    def __init__(self, n_irqs=32, reserved_irqs={}):
        SoCLocHandler.__init__(self, "IRQ", n_locs=n_irqs)
        self.logger = logging.getLogger("SoCIRQHandler")
        self.logger.info("Creating IRQ Handler...")
        self.enabled = False

        # Check IRQ Number.
        if n_irqs > 32:
            self.logger.error("Unsupported IRQs number: {} supported are: {:s}".format(
                colorer(n_irqs, color="red"), colorer("Up to 32", color="green")))
            raise SoCError()

        # Create IRQ Handler.
        self.logger.info("IRQ Handler (up to {} Locations).".format(colorer(n_irqs)))

        # Adding reserved IRQs.
        self.logger.info("Adding {} IRQs...".format(colorer("reserved", color="cyan")))
        for name, n in reserved_irqs.items():
            self.add(name, n)

        self.logger.info("IRQ Handler {}.".format(colorer("created", color="green")))

    # Enable ---------------------------------------------------------------------------------------
    def enable(self):
        self.enabled = True

    # Add ------------------------------------------------------------------------------------------
    def add(self, name, *args, **kwargs):
        if self.enabled:
            SoCLocHandler.add(self, name, *args, **kwargs)
        else:
            self.logger.error("Attempted to add {} IRQ but SoC does {}.".format(
                colorer(name), colorer("not support IRQs", color="red")))
            raise SoCError()

    # Str ------------------------------------------------------------------------------------------
    def __str__(self):
        r ="IRQ Handler (up to {} Locations).\n".format(colorer(self.n_locs))
        r += SoCLocHandler.__str__(self)
        r = r[:-1]
        return r

# SoCController ------------------------------------------------------------------------------------

class SoCController(LiteXModule):
    def __init__(self, with_reset=True, with_scratch=True, with_errors=True):
        if with_reset:
            self._reset = CSRStorage(fields=[
                CSRField("soc_rst", size=1, offset=0, pulse=True, description="""Write `1` to this register to reset the full SoC (Pulse Reset)"""),
                CSRField("cpu_rst", size=1, offset=1,             description="""Write `1` to this register to reset the CPU(s) of the SoC (Hold Reset)"""),
            ])
        if with_scratch:
            self._scratch = CSRStorage(32, reset=0x12345678, description="""
                Use this register as a scratch space to verify that software read/write accesses
                to the Wishbone/CSR bus are working correctly. The initial reset value of 0x1234578
                can be used to verify endianness.""")
        if with_errors:
            self._bus_errors = CSRStatus(32, description="Total number of Wishbone bus errors (timeouts) since start.")

        # # #

        # Reset
        if with_reset:
            self.soc_rst = self._reset.fields.soc_rst
            self.cpu_rst = self._reset.fields.cpu_rst

        # Errors
        if with_errors:
            self.bus_error = Signal()
            bus_errors     = Signal(32)
            self.sync += [
                If(bus_errors != (2**len(bus_errors)-1),
                    If(self.bus_error, bus_errors.eq(bus_errors + 1))
                )
            ]
            self.comb += self._bus_errors.status.eq(bus_errors)

# SoC ----------------------------------------------------------------------------------------------

class SoC(LiteXModule, SoCCoreCompat):
    mem_map = {}
    def __init__(self, platform, sys_clk_freq,
        bus_standard         = "wishbone",
        bus_data_width       = 32,
        bus_address_width    = 32,
        bus_timeout          = 1e6,
        bus_bursting         = False,
        bus_interconnect     = "shared",
        bus_reserved_regions = {},

        csr_data_width       = 32,
        csr_address_width    = 14,
        csr_paging           = 0x800,
        csr_ordering         = "big",
        csr_reserved_csrs    = {},

        irq_n_irqs           = 32,
        irq_reserved_irqs    = {},
        ):
        # Create logging config only if not already configured.
        if not len(logging.root.handlers):
            logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("SoC")
        self.logger.info(colorer("        __   _ __      _  __  ", color="bright"))
        self.logger.info(colorer("       / /  (_) /____ | |/_/  ", color="bright"))
        self.logger.info(colorer("      / /__/ / __/ -_)>  <    ", color="bright"))
        self.logger.info(colorer("     /____/_/\\__/\\__/_/|_|  ", color="bright"))
        self.logger.info(colorer("  Build your hardware, easily!", color="bright"))

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Creating SoC... ({})".format(build_time())))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info("FPGA device : {}.".format(platform.device))
        self.logger.info("System clock: {:3.3f}MHz.".format(sys_clk_freq/1e6))

        # SoC attributes ---------------------------------------------------------------------------
        self.platform     = platform
        self.sys_clk_freq = int(sys_clk_freq) # Do conversion to int here to allow passing float to SoC.
        self.constants    = {}
        self.csr_regions  = {}

        # Set Top-Level to LiteXContext.
        LiteXContext.top = self

        # SoC Bus Handler --------------------------------------------------------------------------
        self.bus = SoCBusHandler(
            standard         = bus_standard,
            data_width       = bus_data_width,
            address_width    = bus_address_width,
            timeout          = bus_timeout,
            bursting         = bus_bursting,
            interconnect     = bus_interconnect,
            reserved_regions = bus_reserved_regions,
           )

        # SoC Bus Handler --------------------------------------------------------------------------
        self.csr = SoCCSRHandler(
            data_width    = csr_data_width,
            address_width = csr_address_width,
            alignment     = 32,
            paging        = csr_paging,
            ordering      = csr_ordering,
            reserved_csrs = csr_reserved_csrs,
        )

        # SoC IRQ Handler --------------------------------------------------------------------------
        self.irq = SoCIRQHandler(
            n_irqs        = irq_n_irqs,
            reserved_irqs = irq_reserved_irqs
        )

        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Initial SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

        # SoC Configs ------------------------------------------------------------------------------
        self.add_config("PLATFORM_NAME", platform.name)
        self.add_config("CLOCK_FREQUENCY", int(sys_clk_freq))

    # SoC Helpers ----------------------------------------------------------------------------------
    def check_if_exists(self, name):
        if hasattr(self, name):
            self.logger.error("{} SubModule already {}.".format(
                colorer(name),
                colorer("declared", color="red")))
            raise SoCError()

    def add_constant(self, name, value=None, check_duplicate=True):
        name = name.upper()
        if name in self.constants.keys():
            if check_duplicate:
                self.logger.error("{} Constant already {}.".format(
                    colorer(name),
                    colorer("declared", color="red")))
                raise SoCError()
        self.constants[name] = SoCConstant(value)

    def add_config(self, name, value=None, check_duplicate=True):
        name = "CONFIG_" + name
        self.add_constant(name, value, check_duplicate=check_duplicate)

    def check_bios_requirements(self):
        # Check for required Peripherals.
        for periph in [ "timer0"]:
            if periph not in self.csr.locs.keys():
                self.logger.error("BIOS needs {} peripheral to be {}.".format(
                    colorer(periph),
                    colorer("used", color="red")))
                self.logger.error(self.bus)
                raise SoCError()

        # Check for required Memory Regions.
        for mem in ["rom", "sram"]:
            if mem not in self.bus.regions.keys():
                self.logger.error("BIOS needs {} Region to be {} as Bus or Linker Region.".format(
                    colorer(mem),
                    colorer("defined", color="red")))
                self.logger.error(self.bus)
                raise SoCError()

    # SoC Main Components --------------------------------------------------------------------------

    # Add Controller -------------------------------------------------------------------------------
    def add_controller(self, name="ctrl", **kwargs):
        self.check_if_exists(name)
        self.logger.info("Controller {} {}.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))
        self.add_module(name=name, module=SoCController(**kwargs))

    # Add/Init RAM ---------------------------------------------------------------------------------
    def add_ram(self, name, origin, size, contents=[], mode="rwx"):
        ram_cls = {
            "wishbone": wishbone.SRAM,
            "axi-lite": axi.AXILiteSRAM,
            "axi"     : axi.AXILiteSRAM, # FIXME: Use AXI-Lite for now, create AXISRAM.
        }[self.bus.standard]
        interface_cls = {
            "wishbone": wishbone.Interface,
            "axi-lite": axi.AXILiteInterface,
            "axi"     : axi.AXILiteInterface, # FIXME: Use AXI-Lite for now, create AXISRAM.
        }[self.bus.standard]
        ram_bus = interface_cls(
            data_width    = self.bus.data_width,
            address_width = self.bus.address_width,
            bursting      = self.bus.bursting
        )
        ram = ram_cls(size, bus=ram_bus, init=contents, read_only=("w" not in mode), name=name)
        self.bus.add_slave(name=name, slave=ram.bus, region=SoCRegion(origin=origin, size=size, mode=mode))
        self.check_if_exists(name)
        self.logger.info("RAM {} {} {}.".format(
            colorer(name),
            colorer("added", color="green"),
            self.bus.regions[name]))
        self.add_module(name=name, module=ram)
        if contents != []:
            self.add_config(f"{name}_INIT", 1)

    def init_ram(self, name, contents=[], auto_size=False):
        # RAM Parameters.
        ram        = getattr(self, name)
        ram_region = self.bus.regions[name]
        ram_type   = {
            True  : "ROM",
            False : "RAM",
        }["w" not in ram_region.mode]
        contents_size = 4*len(contents) # FIXME.

        # Size Check.
        if ram_region.size < contents_size:
            self.logger.error("Contents Size ({}) {} {} Size ({}).".format(
                colorer(f"0x{contents_size:x}"),
                colorer("exceeds", color="red"),
                ram_type,
                colorer(f"0x{ram_region.size:x}"),
            ))
            raise SoCError()

        # RAM Initialization.
        self.logger.info("Initializing {} {} with contents (Size: {}).".format(
            ram_type,
            colorer(name),
            colorer(f"0x{contents_size:x}")))
        ram.mem.init = contents

        # RAM Auto-Resize (Optional).
        if auto_size and ("w" not in ram_region.mode):
            self.logger.info("Auto-Resizing {} {} from {} to {}.".format(
                ram_type,
                colorer(name),
                colorer(f"0x{ram_region.size:x}"),
                colorer(f"0x{contents_size:x}")))
            ram.mem.depth = len(contents)

    # Add/Init ROM ---------------------------------------------------------------------------------
    def add_rom(self, name, origin, size, contents=[], mode="rx"):
        self.add_ram(name, origin, size, contents, mode=mode)

    def init_rom(self, name, contents=[], auto_size=True):
        self.init_ram(name, contents, auto_size)

    # Add CSR Bridge -------------------------------------------------------------------------------
    def add_csr_bridge(self, name="csr", origin=None, register=False):
        csr_bridge_cls = {
            "wishbone": wishbone.Wishbone2CSR,
            "axi-lite": axi.AXILite2CSR,
            "axi"     : axi.AXILite2CSR, # Note: CSR is a slow bus so using AXI-Lite is fine.
        }[self.bus.standard]
        bus_bridge_cls = {
            "wishbone": wishbone.Interface,
            "axi-lite": axi.AXILiteInterface,
            "axi"     : axi.AXILiteInterface,
        }[self.bus.standard]
        csr_bridge_name = f"{name}_bridge"
        self.check_if_exists(csr_bridge_name)
        data_width = self.csr.data_width
        csr_bridge = csr_bridge_cls(
            bus_bridge_cls(
                address_width = self.bus.address_width,
                data_width    = data_width),
            bus_csr = csr_bus.Interface(
                address_width = self.csr.address_width,
                data_width    = data_width),
            register = register)
        self.logger.info("CSR Bridge {} {}.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))
        self.add_module(name=csr_bridge_name, module=csr_bridge)
        csr_size   = 2**(self.csr.address_width + 2)
        csr_region = SoCRegion(origin=origin, size=csr_size, cached=False, decode=self.cpu.csr_decode)
        bus_standard = {
            "wishbone": "wishbone",
            "axi-lite": "axi-lite",
            "axi"     : "axi-lite",
        }[self.bus.standard]
        bus = getattr(csr_bridge, bus_standard.replace("-", "_"))
        self.bus.add_slave(name=name, slave=bus, region=csr_region)
        self.csr.add_master(name=name, master=csr_bridge.csr)
        self.add_config("CSR_DATA_WIDTH", self.csr.data_width)
        self.add_config("CSR_ALIGNMENT",  self.csr.alignment)

    # Add CPU --------------------------------------------------------------------------------------
    def add_cpu(self, name="vexriscv", variant="standard", reset_address=None, cfu=None):
        from litex.soc.cores import cpu

        # Check that CPU is supported.
        if name not in cpu.CPUS.keys():
            supported_cpus = []
            cpu_name_length = max([len(cpu_name) for cpu_name in cpu.CPUS.keys()])
            for cpu_name in sorted(cpu.CPUS.keys()):
                cpu_cls  = cpu.CPUS[cpu_name]
                cpu_desc = f"{cpu_cls.family}\t/ {cpu_cls.category}"
                supported_cpus += [f"- {cpu_name}{' '*(cpu_name_length - len(cpu_name))} ({cpu_desc})"]
            self.logger.error("{} CPU {}, supported are: \n{}".format(
                colorer(name),
                colorer("not supported", color="red"),
                colorer("\n".join(supported_cpus))))
            raise SoCError()

        # Add CPU.
        cpu_cls = cpu.CPUS[name]
        if (variant not in cpu_cls.variants) and (cpu_cls is not cpu.CPUNone):
            self.logger.error("{} CPU variant {}, supported are: \n - {}".format(
                colorer(variant),
                colorer("not supported", color="red"),
                colorer("\n - ".join(sorted(cpu_cls.variants)))))
            raise SoCError()
        self.check_if_exists("cpu")
        if cpu_cls is cpu.CPUNone:
            self.cpu = cpu_cls(self.bus.data_width, self.bus.address_width)
        else:
            self.cpu = cpu_cls(self.platform, variant)
        self.logger.info("CPU {} {}.".format(
            colorer(name, color="underline"),
            colorer("added", color="green")))

        # Add optional CFU plugin.
        if "cfu" in variant and hasattr(self.cpu, "add_cfu"):
            self.cpu.add_cfu(cfu_filename=cfu)

        # Update SoC with CPU constraints.
        # IO regions.
        for n, (origin, size) in enumerate(self.cpu.io_regions.items()):
            self.logger.info("CPU {} {} IO Region {} at {} (Size: {}).".format(
                colorer(name, color="underline"),
                colorer("adding", color="cyan"),
                colorer(n),
                colorer(f"0x{origin:08x}"),
                colorer(f"0x{size:08x}")))
            self.bus.add_region("io{}".format(n), SoCIORegion(origin=origin, size=size, cached=False))
        # Mapping.
        if isinstance(self.cpu, cpu.CPUNone):
            # With CPUNone, give priority to User's mapping.
            self.mem_map = {**self.cpu.mem_map, **self.mem_map}
            # With CPUNone, disable IO regions check.
            self.bus.io_regions_check = False
        else:
            # Override User's mapping with CPU constrainted mapping (and warn User).
            for n, origin in self.cpu.mem_map.items():
                if n in self.mem_map.keys() and self.mem_map[n] != self.cpu.mem_map[n]:
                    self.logger.info("CPU {} {} {} mapping from {} to {}.".format(
                        colorer(name, color="underline"),
                        colorer("overriding", color="cyan"),
                        colorer(n),
                        colorer(f"0x{self.mem_map[n]:08x}"),
                        colorer(f"0x{self.cpu.mem_map[n]:08x}")))
            self.mem_map.update(self.cpu.mem_map)

        # Add Bus Masters/CSR/IRQs.
        if not isinstance(self.cpu, cpu.CPUNone):
            # Reset Address.
            if reset_address is None:
                reset_address = self.mem_map["rom"]
            self.logger.info("CPU {} {} reset address to {}.".format(
                colorer(name, color="underline"),
                colorer("setting", color="cyan"),
                colorer(f"0x{reset_address:08x}")))
            self.cpu.set_reset_address(reset_address)

            # Bus Masters.
            self.logger.info("CPU {} {} Bus Master(s).".format(
                colorer(name, color="underline"),
                colorer("adding", color="cyan")))
            for n, cpu_bus in enumerate(self.cpu.periph_buses):
                self.bus.add_master(name="cpu_bus{}".format(n), master=cpu_bus)

            # Interrupts.
            if hasattr(self.cpu, "interrupt"):
                self.logger.info("CPU {} {} Interrupt(s).".format(
                    colorer(name, color="underline"),
                    colorer("adding", color="cyan")))
                self.irq.enable()
                if hasattr(self.cpu, "reserved_interrupts"):
                    self.cpu.interrupts.update(self.cpu.reserved_interrupts)
                for irq_name, loc in self.cpu.interrupts.items():
                    self.irq.add(irq_name, loc)
                self.add_config("CPU_HAS_INTERRUPT")

            # Create optional DMA Bus (for Cache Coherence).
            if hasattr(self.cpu, "dma_bus"):
                if isinstance(self.cpu.dma_bus, wishbone.Interface):
                    dma_bus_standard = "wishbone"
                elif isinstance(self.cpu.dma_bus, axi.AXILiteInterface):
                    dma_bus_standard = "axi_lite"
                elif isinstance(self.cpu.dma_bus, axi.AXIInterface):
                    dma_bus_standard = "axi"
                else:
                    raise NotImplementedError
                self.logger.info("CPU {} {} DMA Bus.".format(
                    colorer(name, color="underline"),
                    colorer("adding", color="cyan"))
                )
                self.dma_bus = SoCBusHandler(
                    name             = "SoCDMABusHandler",
                    standard         = dma_bus_standard,
                    data_width       = self.cpu.dma_bus.data_width,
                    address_width    = self.cpu.dma_bus.address_width,
                    bursting         = self.cpu.dma_bus.bursting
                )
                self.dma_bus.add_slave(name="dma", slave=self.cpu.dma_bus, region=SoCRegion(origin=0x00000000, size=0x100000000)) # FIXME: covers lower 4GB only

            # Connect SoCController's reset to CPU reset.
            if hasattr(self, "ctrl"):
                self.comb += self.cpu.reset.eq(
                    # Reset the CPU on...
                    getattr(self.ctrl, "soc_rst", 0) | # Full SoC Reset command...
                    getattr(self.ctrl, "cpu_rst", 0)   # or on CPU Reset command.
                )
            self.add_config("CPU_RESET_ADDR", reset_address)

        # Add CPU's SoC components (if any).
        if hasattr(self.cpu, "add_soc_components"):
            self.logger.info("CPU {} {} SoC components.".format(
                colorer(name, color="underline"),
                colorer("adding", color="cyan")))
            self.cpu.add_soc_components(soc=self)

        # Add constants.
        self.add_config(f"CPU_TYPE_{name}")
        self.add_config(f"CPU_VARIANT_{str(variant.split('+')[0])}")
        self.add_config("CPU_FAMILY",     getattr(self.cpu, "family",     "Unknown"))
        self.add_config("CPU_NAME",       getattr(self.cpu, "name",       "Unknown"))
        self.add_config("CPU_HUMAN_NAME", getattr(self.cpu, "human_name", "Unknown"))
        if hasattr(self.cpu, "nop"):
            self.add_config("CPU_NOP", self.cpu.nop)

    # Add Timer ------------------------------------------------------------------------------------
    def add_timer(self, name="timer0"):
        from litex.soc.cores.timer import Timer
        self.check_if_exists(name)
        self.add_module(name=name, module=Timer())
        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

    # Add Watchdog ---------------------------------------------------------------------------------
    def add_watchdog(self, name="watchdog0", width=32, crg_rst=None, reset_delay=None):
        from litex.soc.cores.watchdog import Watchdog

        if crg_rst is None:
            crg_rst = getattr(self.crg, "rst", None) if hasattr(self, "crg") else None
        if reset_delay is None:
            reset_delay = self.sys_clk_freq

        halted = getattr(self.cpu, "o_halted", None) if hasattr(self, "cpu") else None

        self.check_if_exists(name)
        watchdog = Watchdog(width=width, crg_rst=crg_rst, reset_delay=int(reset_delay), halted=halted)
        self.add_module(name=name, module=watchdog)

        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

    # SoC finalization -----------------------------------------------------------------------------
    def finalize(self):
        if self.finalized:
            return
        # Compat -----------------------------------------------------------------------------------
        SoCCoreCompat.finalize_wb_slaves(self) # FIXME: Deprecate compat and remove.

        # SoC Reset --------------------------------------------------------------------------------
        # Connect soc_rst to CRG's rst if present.
        if hasattr(self, "ctrl") and hasattr(self, "crg"):
            crg_rst = getattr(self.crg, "rst", None)
            if isinstance(crg_rst, Signal):
                self.comb += If(getattr(self.ctrl, "soc_rst", 0), crg_rst.eq(1))

        # SoC CSR bridge ---------------------------------------------------------------------------
        self.add_csr_bridge(
            name     = "csr",
            origin   = self.mem_map["csr"],
            register = hasattr(self, "sdram"),
        )

        # SoC Bus Interconnect ---------------------------------------------------------------------
        self.bus.finalize()
        if hasattr(self, "ctrl") and self.bus.timeout is not None:
            if hasattr(self.ctrl, "bus_error") and hasattr(self.bus._interconnect, "timeout"):
                self.comb += self.ctrl.bus_error.eq(self.bus._interconnect.timeout.error)
        self.add_config("BUS_STANDARD",      self.bus.standard)
        self.add_config("BUS_DATA_WIDTH",    self.bus.data_width)
        self.add_config("BUS_ADDRESS_WIDTH", self.bus.address_width)
        self.add_config("BUS_BURSTING",      int(self.bus.bursting))

        # SoC DMA Bus Interconnect (Cache Coherence) -----------------------------------------------
        if hasattr(self, "dma_bus"):
            self.dma_bus.finalize()
            self.add_config("CPU_HAS_DMA_BUS")

        # SoC Main CSRs collection -----------------------------------------------------------------

        # Collect CSRs created on the Main Module.
        main_csrs = dict()
        for name, obj in self.__dict__.items():
            if isinstance(obj, (CSR, CSRStorage, CSRStatus)):
                main_csrs[name] = obj

        # Add Main CSRs to a "main" Sub-Module and delete it from Main Module.
        if main_csrs:
            self.main = LiteXModule()
            for name, csr in main_csrs.items():
                setattr(self.main, name, csr)
                delattr(self, name)

        # SoC CSR Interconnect ---------------------------------------------------------------------
        self.csr_bankarray = csr_bus.CSRBankArray(self,
            address_map        = self.csr.address_map,
            data_width         = self.csr.data_width,
            address_width      = self.csr.address_width,
            alignment          = self.csr.alignment,
            paging             = self.csr.paging,
            ordering           = self.csr.ordering)
        if len(self.csr.masters):
            self.csr_interconnect = csr_bus.InterconnectShared(
                masters = list(self.csr.masters.values()),
                slaves  = self.csr_bankarray.get_buses())

        # Add CSRs regions.
        for name, csrs, mapaddr, rmap in self.csr_bankarray.banks:
            self.csr.add_region(name, SoCCSRRegion(
                origin   = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busword  = self.csr.data_width,
                obj      = csrs))

        # Add Memory regions.
        for name, memory, mapaddr, mmap in self.csr_bankarray.srams:
            self.csr.add_region(name + "_" + memory.name_override, SoCCSRRegion(
                origin  = (self.bus.regions["csr"].origin + self.csr.paging*mapaddr),
                busword = self.csr.data_width,
                obj     = memory))

        # Sort CSR regions by origin.
        self.csr.regions = {k: v for k, v in sorted(self.csr.regions.items(), key=lambda item: item[1].origin)}

        # Add CSRs / Config items to constants.
        for name, constant in self.csr_bankarray.constants:
            self.add_constant(name + "_" + constant.name, constant.value.value)

        # SoC CPU Reset Address Check --------------------------------------------------------------

        # Check if CPU Reset Address is in a defined Region.
        cpu_reset_address_valid = False
        for name, container in self.bus.regions.items():
            if self.bus.check_region_is_in(
                region    = SoCRegion(origin=self.cpu.reset_address, size=self.bus.data_width//8),
                container = container):
                cpu_reset_address_valid = True
                # If we have a ROM, make the CPU use it.
                if name == "rom":
                    self.cpu.use_rom = True

        # If CPU Reset Address Check is enabled and Reset Address is invalid, raise SoCError.
        if self.cpu.reset_address_check and (not cpu_reset_address_valid):
            self.logger.error("CPU needs {} to be in a {} Region.".format(
                colorer("reset address 0x{:08x}".format(self.cpu.reset_address)),
                colorer("defined", color="red")))
            self.logger.error(self.bus)
            raise SoCError()

        # SoC IRQ Interconnect ---------------------------------------------------------------------
        if hasattr(self, "cpu") and hasattr(self.cpu, "interrupt"):
            self.add_config("CPU_INTERRUPTS", max(self.irq.locs.values()) + 1)
            for name, loc in sorted(self.irq.locs.items()):
                if name in self.cpu.interrupts.keys():
                    continue
                if hasattr(self, name):
                    module = getattr(self, name)
                    ev = None
                    if hasattr(module, "ev"):
                        ev = module.ev
                    elif isinstance(module, EventManager):
                        ev = module
                    else:
                        self.logger.error("EventManager {} in {} SubModule.".format(
                            colorer("not found", color="red"),
                            colorer(name)))
                        raise SoCError()
                    self.comb += self.cpu.interrupt[loc].eq(ev.irq)
                self.add_constant(name + "_INTERRUPT", loc)

        # SoC Infos --------------------------------------------------------------------------------
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("Finalized SoC:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(self.bus)
        if hasattr(self, "dma_bus"):
            self.logger.info(self.dma_bus)
        self.logger.info(self.csr)
        self.logger.info(self.irq)
        self.logger.info(colorer("-"*80, color="bright"))

        # Finalize submodules ----------------------------------------------------------------------
        Module.finalize(self)

        # Compat -----------------------------------------------------------------------------------
        SoCCoreCompat.finalize_csr_regions(self) # FIXME: Deprecate compat and remove.

        # SoC Hierarchy ----------------------------------------------------------------------------
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(colorer("SoC Hierarchy:"))
        self.logger.info(colorer("-"*80, color="bright"))
        self.logger.info(LiteXHierarchyExplorer(top=self, depth=None))
        self.logger.info(colorer("-"*80, color="bright"))

    # SoC build ------------------------------------------------------------------------------------
    def get_build_name(self):
        return getattr(self, "build_name", self.platform.name)

    def build(self, *args, **kwargs):
        self.build_name = kwargs.pop("build_name", self.platform.name)
        if self.build_name[0].isdigit():
            self.build_name = f"_{self.build_name}"
        kwargs.update({"build_name": self.build_name})
        return self.platform.build(self, *args, **kwargs)

# LiteXSoC -----------------------------------------------------------------------------------------

class LiteXSoC(SoC):
    # Add Identifier -------------------------------------------------------------------------------
    def add_identifier(self, name="identifier", identifier="LiteX SoC", with_build_time=True):
        from litex.soc.cores.identifier import Identifier
        self.check_if_exists(name)
        if with_build_time:
            identifier += " " + build_time()
        else:
            self.add_config("BIOS_NO_BUILD_TIME")
        self.add_module(name=name, module=Identifier(identifier))
        self.add_config(name, identifier)

    # Add UART -------------------------------------------------------------------------------------
    def add_uart(self, name="uart", uart_name="serial", uart_pads=None, baudrate=115200, fifo_depth=16, with_dynamic_baudrate=False):
        # Imports.
        from litex.soc.cores.uart import UART, UARTCrossover

        # Core.
        self.check_if_exists(name)
        supported_uarts = [
            "crossover",
            "crossover+uartbone",
            "jtag_uart",
            "sim",
            "stub",
            "stream",
            "uartbone",
            "usb_acm",
            "serial(x)",
        ]
        if uart_pads is None:
            uart_pads_name = "serial" if uart_name == "sim" else uart_name
            uart_pads      = self.platform.request(uart_pads_name, loose=True)
        uart_phy       = None
        uart           = None
        uart_kwargs    = {
            "tx_fifo_depth": fifo_depth,
            "rx_fifo_depth": fifo_depth,
        }
        if (uart_pads is None) and (uart_name not in supported_uarts):
            self.logger.error("{} UART {}, supported are: \n{}.".format(
                colorer(uart_name),
                colorer("not supported/found on board", color="red"),
                colorer("- " + "\n- ".join(supported_uarts))))
            raise SoCError()

        # Crossover.
        if uart_name in ["crossover"]:
            uart = UARTCrossover(**uart_kwargs)

        # Crossover + UARTBone.
        elif uart_name in ["crossover+uartbone"]:
            self.add_uartbone(baudrate=baudrate, with_dynamic_baudrate=with_dynamic_baudrate)
            uart = UARTCrossover(**uart_kwargs)

        # JTAG UART.
        elif uart_name in ["jtag_uart"]:
            from litex.soc.cores.jtag import JTAGPHY
            uart_phy = JTAGPHY(device=self.platform.device, platform=self.platform)
            uart     = UART(uart_phy, **uart_kwargs)

        # Sim.
        elif uart_name in ["sim"]:
            from litex.soc.cores.uart import RS232PHYModel
            uart_phy = RS232PHYModel(uart_pads)
            uart     = UART(uart_phy, **uart_kwargs)

        # Stub / Stream.
        elif uart_name in ["stub", "stream"]:
            uart = UART(tx_fifo_depth=0, rx_fifo_depth=0)
            self.comb += uart.source.ready.eq(uart_name == "stub")

        # UARTBone.
        elif uart_name in ["uartbone"]:
            self.add_uartbone(baudrate=baudrate)

        # USB ACM (with ValentyUSB core).
        elif uart_name in ["usb_acm"]:
            import valentyusb.usbcore.io as usbio
            import valentyusb.usbcore.cpu.cdc_eptri as cdc_eptri
            usb_pads  = self.platform.request("usb")
            usb_iobuf = usbio.IoBuf(usb_pads.d_p, usb_pads.d_n, usb_pads.pullup)
            # Run USB-ACM in sys_usb clock domain similar to sys_clk domain but without sys_rst.
            self.cd_sys_usb = ClockDomain()
            self.comb += self.cd_sys_usb.clk.eq(ClockSignal("sys"))
            uart = ClockDomainsRenamer("sys_usb")(cdc_eptri.CDCUsb(usb_iobuf))

        # Regular UART.
        else:
            from litex.soc.cores.uart import UARTPHY
            uart_phy  = UARTPHY(uart_pads, clk_freq=self.sys_clk_freq, baudrate=baudrate, with_dynamic_baudrate=with_dynamic_baudrate)
            uart      = UART(uart_phy, **uart_kwargs)

        # Add PHY/UART.
        if uart_phy is not None:
            self.add_module(name=f"{name}_phy", module=uart_phy)
        if uart is not None:
            self.add_module(name=name, module=uart)

        # IRQ.
        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)
        else:
            self.add_constant("UART_POLLING", check_duplicate=False)

    # Add UARTbone ---------------------------------------------------------------------------------
    def add_uartbone(self, name="uartbone", uart_name="serial", clk_freq=None, baudrate=115200, cd="sys", with_dynamic_baudrate=False):
        # Imports.
        from litex.soc.cores import uart

        # Core.
        if clk_freq is None:
            clk_freq = self.sys_clk_freq
        self.check_if_exists(name)
        uartbone_phy = uart.UARTPHY(self.platform.request(uart_name), clk_freq, baudrate, with_dynamic_baudrate=with_dynamic_baudrate)
        uartbone     = uart.UARTBone(
            phy           = uartbone_phy,
            clk_freq      = clk_freq,
            cd            = cd,
            address_width = self.bus.address_width)
        self.add_module(name=f"{name}_phy", module=uartbone_phy)
        self.add_module(name=name,          module=uartbone)
        self.bus.add_master(name=name, master=uartbone.wishbone)

    # Add JTAGbone ---------------------------------------------------------------------------------
    def add_jtagbone(self, name="jtagbone", chain=1):
        # Imports.
        from litex.soc.cores import uart
        from litex.soc.cores.jtag import JTAGPHY

        # Check if JTAGBone is supported (SPI only device or no user access).
        if not self.platform.jtag_support:
            self.logger.error("{} {} on {} device.".format(
                colorer("JTAGBone"),
                colorer("not supported", color="red"),
                colorer(self.platform.device)
            ))
            raise SoCError()

        # Core.
        self.check_if_exists(name)
        jtagbone_phy = JTAGPHY(device=self.platform.device, chain=chain, platform=self.platform)
        jtagbone = uart.UARTBone(
            phy           = jtagbone_phy,
            clk_freq      = self.sys_clk_freq,
            address_width = self.bus.address_width
        )
        self.add_module(name=f"{name}_phy", module=jtagbone_phy)
        self.add_module(name=name,          module=jtagbone)
        self.bus.add_master(name=name, master=jtagbone.wishbone)

    # Add SDRAM ------------------------------------------------------------------------------------
    def add_sdram(self, name="sdram", phy=None, module=None, origin=None, size=None,
        with_bist               = False,
        with_soc_interconnect   = True,
        l2_cache_size           = 8192,
        l2_cache_min_data_width = 128,
        l2_cache_reverse        = False,
        l2_cache_full_memory_we = True,
        **kwargs):

        # Imports.
        from litedram.common import LiteDRAMNativePort
        from litedram.core import LiteDRAMCore
        from litedram.frontend.wishbone import LiteDRAMWishbone2Native
        from litedram.frontend.axi import LiteDRAMAXI2Native
        from litedram.frontend.bist import  LiteDRAMBISTGenerator, LiteDRAMBISTChecker

        # LiteDRAM core.
        self.check_if_exists(name)
        sdram = LiteDRAMCore(
            phy             = phy,
            geom_settings   = module.geom_settings,
            timing_settings = module.timing_settings,
            clk_freq        = self.sys_clk_freq,
            **kwargs)
        self.add_module(name=name, module=sdram)

        # Save SPD data to be able to verify it at runtime.
        if hasattr(module, "_spd_data"):
            # Pack the data into words of bus width.
            bytes_per_word = self.bus.data_width // 8
            mem = [0] * math.ceil(len(module._spd_data) / bytes_per_word)
            for i in range(len(mem)):
                for offset in range(bytes_per_word):
                    mem[i] <<= 8
                    if self.cpu.endianness == "little":
                        offset = bytes_per_word - 1 - offset
                    spd_byte = i * bytes_per_word + offset
                    if spd_byte < len(module._spd_data):
                        mem[i] |= module._spd_data[spd_byte]
            self.add_rom(
                name     = f"{name}_spd",
                origin   = self.mem_map.get(f"{name}_spd", None),
                size     = len(module._spd_data),
                contents = mem,
            )

        # LiteDRAM BIST.
        if with_bist:
            sdram_generator = LiteDRAMBISTGenerator(sdram.crossbar.get_port())
            sdram_checker   = LiteDRAMBISTChecker(  sdram.crossbar.get_port())
            self.add_module(name=f"{name}_generator", module=sdram_generator)
            self.add_module(name=f"{name}_checker",   module=sdram_checker)

        if not with_soc_interconnect: return

        # Compute/Check SDRAM size.
        sdram_size = 2**(module.geom_settings.bankbits +
                         module.geom_settings.rowbits +
                         module.geom_settings.colbits)*phy.settings.nranks*phy.settings.databits//8
        if size is not None:
            sdram_size = min(sdram_size, size)

        # Add SDRAM region.
        main_ram_region = SoCRegion(
            origin = self.mem_map.get("main_ram", origin),
            size   = sdram_size,
            mode   = "rwx")
        self.bus.add_region("main_ram", main_ram_region)

        # Add CPU's direct memory buses (if not already declared) ----------------------------------
        if hasattr(self.cpu, "add_memory_buses"):
            self.cpu.add_memory_buses(
                address_width = 32,
                data_width    = sdram.crossbar.controller.data_width
            )

        # Connect CPU's direct memory buses to LiteDRAM --------------------------------------------
        if len(self.cpu.memory_buses):
            # When CPU has at least a direct memory bus, connect them directly to LiteDRAM.
            for mem_bus in self.cpu.memory_buses:
                # Request a LiteDRAM native port.
                port = sdram.crossbar.get_port()
                port.data_width = 2**int(math.log2(port.data_width)) # Round to nearest power of 2.

                # Check if bus is an AXI bus and connect it.
                if isinstance(mem_bus, axi.AXIInterface):
                    data_width_ratio = int(port.data_width/mem_bus.data_width)
                    if data_width_ratio != 1:
                        self.logger.warning("Converting MemBus({}) data width to LiteDRAM({}).".format(
                            colorer(mem_bus.data_width, color="yellow"),
                            colorer(port.data_width,    color="yellow")))
                    # If same data_width, connect it directly.
                    if data_width_ratio == 1:
                        self.submodules += LiteDRAMAXI2Native(
                            axi          = mem_bus,
                            port         = port,
                            base_address = self.bus.regions["main_ram"].origin
                        )
                    # UpConvert.
                    elif data_width_ratio > 1:
                        axi_port = axi.AXIInterface(
                            data_width = port.data_width,
                            id_width   = len(mem_bus.aw.id),
                        )
                        self.submodules += axi.AXIUpConverter(
                            axi_from = mem_bus,
                            axi_to   = axi_port,
                        )
                        self.submodules += LiteDRAMAXI2Native(
                            axi          = axi_port,
                            port         = port,
                            base_address = self.bus.regions["main_ram"].origin
                        )
                    # DownConvert. FIXME: Pass through Wishbone for now, create/use native AXI converter.
                    else:
                        mem_wb  = wishbone.Interface(
                            data_width = self.cpu.mem_axi.data_width,
                            adr_width  = 32-log2_int(mem_bus.data_width//8),
                            addressing = "word",
                        )
                        mem_a2w = axi.AXI2Wishbone(
                            axi          = mem_bus,
                            wishbone     = mem_wb,
                            base_address = 0)
                        self.submodules += mem_a2w
                        litedram_wb = wishbone.Interface(port.data_width)
                        self.submodules += LiteDRAMWishbone2Native(
                            wishbone     = litedram_wb,
                            port         = port,
                            base_address = self.bus.regions["main_ram"].origin)
                        self.submodules += wishbone.Converter(mem_wb, litedram_wb)

                # Check if bus is a Native bus and connect it.
                if isinstance(mem_bus, LiteDRAMNativePort):
                    # If same data_width, connect it directly.
                    if port.data_width == mem_bus.data_width:
                        self.comb += mem_bus.cmd.connect(port.cmd)
                        self.comb += mem_bus.wdata.connect(port.wdata)
                        self.comb += port.rdata.connect(mem_bus.rdata)
                    # Else raise Error.
                    else:
                        raise NotImplementedError

        # Connect Main bus to LiteDRAM (with optional L2 Cache) ------------------------------------
        connect_main_bus_to_dram = (
            # No memory buses.
            (not len(self.cpu.memory_buses)) or
            # Memory buses but no DMA bus.
            (len(self.cpu.memory_buses) and not hasattr(self.cpu, "dma_bus"))
        )
        if connect_main_bus_to_dram:
            # Request a LiteDRAM native port.
            port = sdram.crossbar.get_port()
            port.data_width = 2**int(math.log2(port.data_width)) # Round to nearest power of 2.

            # Create Wishbone Slave.
            wb_sdram = wishbone.Interface(data_width=self.bus.data_width, address_width=32, addressing="word")
            self.bus.add_slave(name="main_ram", slave=wb_sdram)

            # L2 Cache
            if l2_cache_size != 0:
                # Insert L2 cache inbetween Wishbone bus and LiteDRAM
                l2_cache_size = max(l2_cache_size, int(2*port.data_width/8)) # Use minimal size if lower
                l2_cache_size = 2**int(math.log2(l2_cache_size))                  # Round to nearest power of 2
                l2_cache_data_width = max(port.data_width, l2_cache_min_data_width)
                l2_cache = wishbone.Cache(
                    cachesize = l2_cache_size//4,
                    master    = wb_sdram,
                    slave     = wishbone.Interface(data_width=l2_cache_data_width, address_width=32, addressing="word"),
                    reverse   = l2_cache_reverse)
                if l2_cache_full_memory_we:
                    l2_cache = FullMemoryWE()(l2_cache)
                self.l2_cache = l2_cache
                litedram_wb = self.l2_cache.slave
                self.add_config("L2_SIZE", l2_cache_size)
            else:
                litedram_wb = wishbone.Interface(data_width=port.data_width, address_width=32, addressing="word")
                self.submodules += wishbone.Converter(wb_sdram, litedram_wb)

            # Wishbone Slave <--> LiteDRAM bridge.
            self.wishbone_bridge = LiteDRAMWishbone2Native(
                wishbone     = litedram_wb,
                port         = port,
                base_address = self.bus.regions["main_ram"].origin
            )

    # Add Ethernet ---------------------------------------------------------------------------------
    def add_ethernet(self, name="ethmac", phy=None, phy_cd="eth", dynamic_ip=False, software_debug=False,
        data_width              = 8,
        nrxslots                = 2, rxslots_read_only  = True,
        ntxslots                = 2, txslots_write_only = False,
        full_memory_we          = False,
        with_timestamp          = False,
        with_timing_constraints = True,
        local_ip                = None,
        remote_ip               = None,
        mac_address             = None):
        # Imports
        from liteeth.mac import LiteEthMAC
        from liteeth.phy.model import LiteEthPHYModel

        # MAC.
        assert data_width in [8, 32, 64]
        with_sys_datapath = (data_width == 32)
        self.check_if_exists(name)
        if with_timestamp:
            self.timer0.add_uptime()
        ethmac = LiteEthMAC(
            phy               = phy,
            dw                = {8: 32, 32: 32, 64: 64}[data_width],
            interface         = "wishbone",
            endianness        = self.cpu.endianness,
            nrxslots          = nrxslots, rxslots_read_only  = rxslots_read_only,
            ntxslots          = ntxslots, txslots_write_only = txslots_write_only,
            timestamp         = None if not with_timestamp else self.timer0.uptime_cycles,
            full_memory_we    = full_memory_we,
            with_preamble_crc = not software_debug,
            with_sys_datapath = with_sys_datapath)
        if not with_sys_datapath:
            # Use PHY's eth_tx/eth_rx clock domains.
            ethmac = ClockDomainsRenamer({
                "eth_tx": phy_cd + "_tx",
                "eth_rx": phy_cd + "_rx"})(ethmac)
        self.add_module(name=name, module=ethmac)

        # Compute Regions size and add it to the SoC.
        ethmac_rx_region_size = ethmac.rx_slots.constant*ethmac.slot_size.constant
        ethmac_tx_region_size = ethmac.tx_slots.constant*ethmac.slot_size.constant
        ethmac_region_size    = ethmac_rx_region_size + ethmac_tx_region_size
        self.bus.add_region(name, SoCRegion(
            origin = self.mem_map.get(name, None),
            size   = ethmac_region_size,
            linker = True,
            cached = False,
        ))
        ethmac_rx_region = SoCRegion(
            origin = self.bus.regions[name].origin + 0,
            size   = ethmac_rx_region_size,
            linker = True,
            cached = False,
        )
        self.bus.add_slave(name=f"{name}_rx", slave=ethmac.bus_rx, region=ethmac_rx_region)
        ethmac_tx_region = SoCRegion(
            origin = self.bus.regions[name].origin + ethmac_rx_region_size,
            size   = ethmac_tx_region_size,
            linker = True,
            cached = False,
        )
        self.bus.add_slave(name=f"{name}_tx", slave=ethmac.bus_tx, region=ethmac_tx_region)

        # Add IRQs (if enabled).
        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

        # Dynamic IP (if enabled).
        if dynamic_ip:
            assert local_ip is None
            self.add_constant("ETH_DYNAMIC_IP")

        # Local/Remote IP Configuration (optional).
        if local_ip:
            add_ip_address_constants(self, "LOCALIP", local_ip)
        if remote_ip:
            add_ip_address_constants(self, "REMOTEIP", remote_ip)
        if mac_address:
            add_mac_address_constants(self, "MACADDR", mac_address)
        

        # Software Debug
        if software_debug:
            self.add_constant("ETH_UDP_TX_DEBUG")
            self.add_constant("ETH_UDP_RX_DEBUG")

        # Timing constraints
        if with_timing_constraints:
            eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
            eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
            if not isinstance(phy, LiteEthPHYModel) and not getattr(phy, "model", False):
                self.platform.add_period_constraint(eth_rx_clk, 1e9/phy.rx_clk_freq)
                if not eth_rx_clk is eth_tx_clk:
                    self.platform.add_period_constraint(eth_tx_clk, 1e9/phy.tx_clk_freq)
                    self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk)
                else:
                    self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk)

    # Add Etherbone --------------------------------------------------------------------------------
    def add_etherbone(self, name="etherbone", phy=None, phy_cd="eth", data_width=8,
        mac_address             = 0x10e2d5000000,
        ip_address              = "192.168.1.50",
        arp_entries             = 1,
        udp_port                = 1234,
        buffer_depth            = 16,
        with_ip_broadcast       = True,
        with_timing_constraints = True,
        with_ethmac             = False,
        ethmac_address          = 0x10e2d5000001,
        ethmac_local_ip         = "192.168.1.51",
        ethmac_remote_ip        = "192.168.1.100"):

        # Imports
        from liteeth.core import LiteEthUDPIPCore
        from liteeth.frontend.etherbone import LiteEthEtherbone
        from liteeth.phy.model import LiteEthPHYModel

        # Core
        assert data_width in [8, 32, 64]
        with_sys_datapath = (data_width == 32)
        self.check_if_exists(name + "_ethcore")
        ethcore = LiteEthUDPIPCore(
            phy         = phy,
            mac_address = mac_address,
            ip_address  = ip_address,
            clk_freq    = self.clk_freq,
            arp_entries = arp_entries,
            dw          = data_width,
            with_ip_broadcast = with_ip_broadcast,
            with_sys_datapath = with_sys_datapath,
            interface   = {True :            "hybrid", False: "crossbar"}[with_ethmac],
            endianness  = {True : self.cpu.endianness, False:      "big"}[with_ethmac],
        )
        if not with_sys_datapath:
            # Use PHY's eth_tx/eth_rx clock domains.
            ethcore = ClockDomainsRenamer({
                "eth_tx": phy_cd + "_tx",
                "eth_rx": phy_cd + "_rx",
                "sys"   : {True: "sys", False: phy_cd + "_rx"}[with_ethmac],
            })(ethcore)
        self.add_module(name=f"ethcore_{name}", module=ethcore)

        etherbone_cd = "sys"
        if not with_sys_datapath:
            # Create Etherbone clock domain and run it from sys clock domain.
            etherbone_cd = name
            setattr(self, f"cd_{name}", ClockDomain(name))
            self.comb += getattr(self, f"cd_{name}").clk.eq(ClockSignal("sys"))
            self.comb += getattr(self, f"cd_{name}").rst.eq(ResetSignal("sys"))

        # Etherbone
        self.check_if_exists(name)
        etherbone = LiteEthEtherbone(ethcore.udp, udp_port, buffer_depth=buffer_depth, cd=etherbone_cd)
        self.add_module(name=name, module=etherbone)
        self.bus.add_master(name=name, master=etherbone.wishbone.bus)

        # Timing constraints
        if with_timing_constraints:
            eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
            eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
            if not isinstance(phy, LiteEthPHYModel) and not getattr(phy, "model", False):
                self.platform.add_period_constraint(eth_rx_clk, 1e9/phy.rx_clk_freq)
                if not eth_rx_clk is eth_tx_clk:
                    self.platform.add_period_constraint(eth_tx_clk, 1e9/phy.tx_clk_freq)
                    self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk)
                else:
                    self.platform.add_false_path_constraints(self.crg.cd_sys.clk, eth_rx_clk)

        # Ethernet MAC (CPU).
        if with_ethmac:
            assert mac_address != ethmac_address
            assert ip_address  != ethmac_local_ip

            self.check_if_exists("ethmac")
            ethcore.autocsr_exclude = {"mac"}
            # Software Interface.
            self.ethmac = ethmac = ethcore.mac
            ethmac_rx_region_size = ethmac.rx_slots.constant*ethmac.slot_size.constant
            ethmac_tx_region_size = ethmac.tx_slots.constant*ethmac.slot_size.constant
            ethmac_region_size    = ethmac_rx_region_size + ethmac_tx_region_size
            self.bus.add_region("ethmac", SoCRegion(
                origin = self.mem_map.get("ethmac", None),
                size   = ethmac_region_size,
                linker = True,
                cached = False,
            ))
            ethmac_rx_region = SoCRegion(
                origin = self.bus.regions["ethmac"].origin + 0,
                size   = ethmac_rx_region_size,
                linker = True,
                cached = False,
            )
            self.bus.add_slave(name=f"ethmac_rx", slave=ethmac.bus_rx, region=ethmac_rx_region)
            ethmac_tx_region = SoCRegion(
                origin = self.bus.regions["ethmac"].origin + ethmac_rx_region_size,
                size   = ethmac_tx_region_size,
                linker = True,
                cached = False,
            )
            self.bus.add_slave(name=f"ethmac_tx", slave=ethmac.bus_tx, region=ethmac_tx_region)

            # Add IRQs (if enabled).
            if self.irq.enabled:
                self.irq.add("ethmac", use_loc_if_exists=True)

            self.add_constant("ETH_PHY_NO_RESET") # Disable reset from BIOS to avoid disabling Hardware Interface.

            add_ip_address_constants(self,  "LOCALIP",  ethmac_local_ip)
            add_ip_address_constants(self,  "REMOTEIP", ethmac_remote_ip)
            add_mac_address_constants(self, "MACADDR",  ethmac_address)

    # Add I2C Master -------------------------------------------------------------------------------
    def add_i2c_master(self, name="i2cmaster", pads=None, **kwargs):
        # Imports.
        from litei2c import LiteI2C

        if "with_irq" not in kwargs and self.irq.enabled and name in self.irq.locs.keys():
            # If IRQ is enabled, use with_irq.
            kwargs["with_irq"] = True

        # Core.
        self.check_if_exists(name)
        if pads is None:
            pads = self.platform.request(name)
        i2c = LiteI2C(self.sys_clk_freq, pads=pads, **kwargs)
        self.add_module(name=name, module=i2c)

        if hasattr(i2c, "ev") and self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

    # Add SPI Master --------------------------------------------------------------------------------
    def add_spi_master(self, name="spimaster", pads=None, data_width=8, spi_clk_freq=1e6, with_clk_divider=True, **kwargs):
        # Imports.
        from litex.soc.cores.spi import SPIMaster

        self.check_if_exists(f"{name}")

        spi_clk_freq = int(spi_clk_freq)

        if pads is None:
            pads = self.platform.request(name)

        spim = SPIMaster(pads, data_width, self.sys_clk_freq, spi_clk_freq, **kwargs)

        if with_clk_divider:
            spim.add_clk_divider()

        self.add_module(name=f"{name}", module=spim)

        self.add_constant(f"{name}_FREQUENCY",     spi_clk_freq)
        self.add_constant(f"{name}_DATA_WIDTH",     data_width)
        self.add_constant(f"{name}_MAX_CS",    len(pads.cs_n))

    # Add SPI Flash --------------------------------------------------------------------------------
    def add_spi_flash(self, name="spiflash", mode="4x", clk_freq=20e6, module=None, phy=None, rate="1:1", software_debug=False, **kwargs):
        # Imports.
        from litespi import LiteSPI
        from litespi.phy.generic import LiteSPIPHY
        from litespi.opcodes import SpiNorFlashOpCodes

        # Checks/Parameters.
        assert mode in ["1x", "4x"]
        default_divisor = math.ceil(self.sys_clk_freq/(2*clk_freq)) - 1
        clk_freq        = int(self.sys_clk_freq/(2*(default_divisor + 1)))

        if "master_with_irq" not in kwargs and self.irq.enabled and name in self.irq.locs.keys():
            # If IRQ is enabled, use master_with_irq.
            kwargs["master_with_irq"] = True

        # PHY.
        spiflash_phy = phy
        if spiflash_phy is None:
            spiflash_pads = self.platform.request(name if mode == "1x" else name + mode)
            spiflash_phy = LiteSPIPHY(spiflash_pads, module, device=self.platform.device, default_divisor=default_divisor, rate=rate)

        # Core.
        self.check_if_exists(name)
        spiflash = LiteSPI(spiflash_phy, mmap_endianness=self.cpu.endianness, **kwargs)
        spiflash.add_module(name="phy", module=spiflash_phy)
        self.add_module(name=name, module=spiflash)
        spiflash_region = SoCRegion(origin=self.mem_map.get(name, None), size=module.total_size)
        self.bus.add_slave(name=name, slave=spiflash.bus, region=spiflash_region, strip_origin=True)

        if hasattr(spiflash, "ev") and self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

        # Constants.
        self.add_constant(f"{name}_PHY_FREQUENCY",     clk_freq)
        self.add_constant(f"{name}_MODULE_NAME",       module.name)
        self.add_constant(f"{name}_MODULE_TOTAL_SIZE", module.total_size)
        self.add_constant(f"{name}_MODULE_PAGE_SIZE",  module.page_size)
        if mode in [ "4x" ]:
            if SpiNorFlashOpCodes.READ_1_1_4 in module.supported_opcodes:
                self.add_constant(f"{name}_MODULE_QUAD_CAPABLE")
            if SpiNorFlashOpCodes.READ_4_4_4 in module.supported_opcodes:
                self.add_constant(f"{name}_MODULE_QPI_CAPABLE")
        if software_debug:
            self.add_constant(f"{name}_DEBUG")

    # Add SPI RAM --------------------------------------------------------------------------------
    def add_spi_ram(self, name="spiram", mode="4x", clk_freq=20e6, module=None, phy=None, rate="1:1", software_debug=False,
        l2_cache_size           = 8192,
        l2_cache_reverse        = False,
        l2_cache_full_memory_we = True,
        **kwargs):
        # Imports.
        from litespi import LiteSPI
        from litespi.phy.generic import LiteSPIPHY
        from litespi.opcodes import SpiNorFlashOpCodes

        # Checks/Parameters.
        assert mode in ["1x", "4x"]
        default_divisor = math.ceil(self.sys_clk_freq/(2*clk_freq)) - 1
        clk_freq        = int(self.sys_clk_freq/(2*(default_divisor + 1)))

        if "master_with_irq" not in kwargs and self.irq.enabled and name in self.irq.locs.keys():
            # If IRQ is enabled, use master_with_irq.
            kwargs["master_with_irq"] = True

        # PHY.
        spiram_phy = phy
        if spiram_phy is None:
            self.check_if_exists(f"{name}_phy")
            spiram_pads = self.platform.request(name if mode == "1x" else name + mode)
            spiram_phy = LiteSPIPHY(spiram_pads, module, device=self.platform.device, default_divisor=default_divisor, rate=rate)

        # Core.
        self.check_if_exists(f"{name}_mmap")
        spiram = LiteSPI(spiram_phy, mmap_endianness=self.cpu.endianness, with_mmap_write=True, **kwargs)
        spiram.add_module(name="phy", module=spiram_phy)
        self.add_module(name=name, module=spiram)
        spiram_region = SoCRegion(origin=self.mem_map.get(name, None), size=module.total_size)
        
        # Create Wishbone Slave.
        wb_spiram = wishbone.Interface(data_width=32, address_width=32, addressing="word")
        self.bus.add_slave(name=name, slave=wb_spiram, region=spiram_region, strip_origin=True)
        
        # L2 Cache
        if l2_cache_size != 0:
            # Insert L2 cache inbetween Wishbone bus and LiteSPI
            l2_cache_size = max(l2_cache_size, int(2*32/8))              # Use minimal size if lower
            l2_cache_size = 2**int(math.log2(l2_cache_size))                  # Round to nearest power of 2
            l2_cache = wishbone.Cache(
                cachesize = l2_cache_size//4,
                master    = wb_spiram,
                slave     = spiram.bus,
                reverse   = l2_cache_reverse)
            if l2_cache_full_memory_we:
                l2_cache = FullMemoryWE()(l2_cache)
            self.l2_cache = l2_cache
            self.add_config("L2_SIZE", l2_cache_size)
        else:
            self.submodules += wishbone.Converter(wb_spiram, spiram.bus)

        if hasattr(spiram, "ev") and self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

        # Constants.
        self.add_constant(f"{name}_PHY_FREQUENCY",     clk_freq)
        self.add_constant(f"{name}_MODULE_NAME",       module.name)
        self.add_constant(f"{name}_MODULE_TOTAL_SIZE", module.total_size)
        self.add_constant(f"{name}_MODULE_PAGE_SIZE",  module.page_size)
        if mode in [ "4x" ]:
            if SpiNorFlashOpCodes.READ_1_1_4 in module.supported_opcodes:
                self.add_constant(f"{name}_MODULE_QUAD_CAPABLE")
            if SpiNorFlashOpCodes.READ_4_4_4 in module.supported_opcodes:
                self.add_constant(f"{name}_MODULE_QPI_CAPABLE")
        if software_debug:
            self.add_constant(f"{name}_DEBUG")

    # Add SPI SDCard -------------------------------------------------------------------------------
    def add_spi_sdcard(self, name="spisdcard", spi_clk_freq=400e3, with_tristate=False, software_debug=False):
        # Imports.
        from migen.fhdl.specials import Tristate
        from litex.soc.cores.spi import SPIMaster

        # Pads.
        spi_sdcard_pads = self.platform.request(name)
        if hasattr(spi_sdcard_pads, "rst"):
            self.comb += spi_sdcard_pads.rst.eq(0)

        # Tristate (Optional).
        if with_tristate:
            tristate = Signal()
            spi_sdcard_tristate_pads = spi_sdcard_pads
            spi_sdcard_pads          = Record([("clk", 1), ("cs_n", 1), ("mosi", 1), ("miso", 1)])
            self.specials += Tristate(spi_sdcard_tristate_pads.clk,  spi_sdcard_pads.clk,  ~tristate)
            self.specials += Tristate(spi_sdcard_tristate_pads.cs_n, spi_sdcard_pads.cs_n, ~tristate)
            self.specials += Tristate(spi_sdcard_tristate_pads.mosi, spi_sdcard_pads.mosi, ~tristate)
            self.comb += spi_sdcard_pads.miso.eq(spi_sdcard_tristate_pads.miso)

        # Core.
        self.check_if_exists(name)
        spisdcard = SPIMaster(
            pads         = spi_sdcard_pads,
            data_width   = 8,
            sys_clk_freq = self.sys_clk_freq,
            spi_clk_freq = spi_clk_freq,
        )
        spisdcard.add_clk_divider()
        self.add_module(name=name, module=spisdcard)

        # Debug.
        if software_debug:
            self.add_constant("SPISDCARD_DEBUG")

    # Add SDCard -----------------------------------------------------------------------------------
    def add_sdcard(self, name="sdcard", sdcard_name="sdcard", software_debug=False, **kwargs):
        # Imports.
        from litesdcard.emulator import SDEmulator
        from litesdcard.phy import SDPHY
        from litesdcard.core import SDCore
        from litesdcard.frontend.dma import SDBlock2MemDMA, SDMem2BlockDMA

        class LiteSDCard(LiteXModule):
            def __init__(self, soc, name="sdcard", mode="read+write", use_emulator=False):
                # Checks.
                assert mode in ["read", "write", "read+write"]

                # Emulator / Pads.
                if use_emulator:
                    self.sdemulator = SDEmulator(soc.platform)
                    pads = self.sdemulator.pads
                else:
                    pads = soc.platform.request(name)

                # Core.
                self.phy = phy = SDPHY(pads, soc.platform.device, soc.sys_clk_freq, cmd_timeout=10e-1, data_timeout=10e-1)
                self.core = core = SDCore(phy)

                # Block2Mem DMA.
                if "read" in mode:
                    bus = wishbone.Interface(
                        data_width = soc.bus.data_width,
                        adr_width  = soc.bus.get_address_width(standard="wishbone"),
                        addressing = "word",
                    )
                    self.block2mem = block2mem = SDBlock2MemDMA(bus=bus, endianness=soc.cpu.endianness)
                    self.comb += core.source.connect(block2mem.sink)
                    dma_bus = getattr(soc, "dma_bus", soc.bus)
                    dma_bus.add_master(master=bus)

                # Mem2Block DMA.
                if "write" in mode:
                    bus = wishbone.Interface(
                        data_width = soc.bus.data_width,
                        adr_width  = soc.bus.get_address_width(standard="wishbone"),
                        addressing = "word",
                    )
                    self.mem2block = mem2block = SDMem2BlockDMA(bus=bus, endianness=soc.cpu.endianness)
                    self.comb += mem2block.source.connect(core.sink)
                    dma_bus = getattr(soc, "dma_bus", soc.bus)
                    dma_bus.add_master(master=bus)

                # Interrupts.
                self.ev = ev = EventManager()
                ev.card_detect = EventSourcePulse(description="SDCard has been ejected/inserted.")
                if "read" in mode:
                    ev.block2mem_dma = EventSourcePulse(description="Block2Mem DMA terminated.")
                if "write" in mode:
                    ev.mem2block_dma = EventSourcePulse(description="Mem2Block DMA terminated.")
                ev.cmd_done  = EventSourceLevel(description="Command completed.")
                ev.finalize()
                if "read" in mode:
                    self.comb += ev.block2mem_dma.trigger.eq(block2mem.irq)
                if "write" in mode:
                    self.comb += ev.mem2block_dma.trigger.eq(mem2block.irq)
                self.comb += [
                    ev.card_detect.trigger.eq(phy.card_detect_irq),
                    ev.cmd_done.trigger.eq(core.cmd_event.fields.done)
                ]

        self.check_if_exists(name)
        sdcard = LiteSDCard(self, name=sdcard_name, **kwargs)
        self.add_module(name=name, module=sdcard)

        if self.irq.enabled:
            self.irq.add(name, use_loc_if_exists=True)

        # Debug.
        if software_debug:
            self.add_constant(f"{name}_DEBUG")

    # Add SATA -------------------------------------------------------------------------------------
    def add_sata(self, name="sata", phy=None, mode="read+write", with_identify=True, with_bist=False):
        # Imports.
        from litesata.core                 import LiteSATACore
        from litesata.frontend.arbitration import LiteSATACrossbar
        from litesata.frontend.identify    import LiteSATAIdentify, LiteSATAIdentifyCSR
        from litesata.frontend.bist        import LiteSATABIST
        from litesata.frontend.dma         import LiteSATASector2MemDMA, LiteSATAMem2SectorDMA

        # Checks.
        assert mode in ["read", "write", "read+write"]
        sata_clk_freqs = {
            "gen1":  75e6,
            "gen2": 150e6,
            "gen3": 300e6,
        }
        sata_clk_freq = sata_clk_freqs[phy.gen]
        assert self.clk_freq >= sata_clk_freq/2 # FIXME: /2 for 16-bit data-width, add support for 32-bit.

        # Core.
        self.check_if_exists(f"{name}_core")
        sata_core = LiteSATACore(phy)
        self.add_module(name=f"{name}_core", module=sata_core)

        # Crossbar.
        self.check_if_exists(f"{name}_crossbar")
        sata_crossbar = LiteSATACrossbar(sata_core)
        self.add_module(name=f"{name}_crossbar", module=sata_crossbar)

        # BIST.
        if with_bist:
            sata_bist =  LiteSATABIST(sata_crossbar, with_csr=True)
            self.add_module(name=f"{name}_bist", module=sata_bist)

        # Identify.
        if with_identify:
            self.check_if_exists(f"{name}_identify")
            _sata_identify = LiteSATAIdentify(sata_crossbar.get_port())
            sata_identify  = LiteSATAIdentifyCSR(_sata_identify)
            self.add_module(name=f"{name}_identify", module=sata_identify)

        # Sector2Mem DMA.
        if "read" in mode:
            self.check_if_exists(f"{name}_sector2mem")
            bus = wishbone.Interface(
                data_width = self.bus.data_width,
                adr_width  = self.bus.get_address_width(standard="wishbone"),
                addressing = "word",
            )
            sata_sector2mem = LiteSATASector2MemDMA(
               port       = sata_crossbar.get_port(),
               bus        = bus,
               endianness = self.cpu.endianness,
            )
            self.add_module(name=f"{name}_sector2mem", module=sata_sector2mem)
            dma_bus = getattr(self, "dma_bus", self.bus)
            dma_bus.add_master(name=f"{name}_sector2mem", master=bus)

        # Mem2Sector DMA.
        if "write" in mode:
            self.check_if_exists(f"{name}_mem2sector")
            bus = wishbone.Interface(
                data_width = self.bus.data_width,
                adr_width  = self.bus.get_address_width(standard="wishbone"),
                addressing = "word",
            )
            sata_mem2sector = LiteSATAMem2SectorDMA(
               bus        = bus,
               port       = sata_crossbar.get_port(),
               endianness = self.cpu.endianness,
            )
            self.add_module(name=f"{name}_mem2sector", module=sata_mem2sector)
            dma_bus = getattr(self, "dma_bus", self.bus)
            dma_bus.add_master(name=f"{name}_mem2sector", master=bus)

        # Interrupts.
        self.check_if_exists(f"{name}_irq")
        sata_irq = EventManager()
        self.add_module(name=f"{name}_irq", module=sata_irq)
        if "read" in mode:
            sata_irq.sector2mem_dma = EventSourcePulse(description="Sector2Mem DMA terminated.")
        if "write" in mode:
            sata_irq.mem2sector_dma = EventSourcePulse(description="Mem2Sector DMA terminated.")
        sata_irq.finalize()
        if "read" in mode:
            self.comb += sata_irq.sector2mem_dma.trigger.eq(sata_sector2mem.irq)
        if "write" in mode:
            self.comb += sata_irq.mem2sector_dma.trigger.eq(sata_mem2sector.irq)
        if self.irq.enabled:
            self.irq.add(f"{name}_irq", use_loc_if_exists=True)

        # Timing constraints.
        self.platform.add_period_constraint(phy.crg.cd_sata_tx.clk, 1e9/sata_clk_freq)
        self.platform.add_period_constraint(phy.crg.cd_sata_rx.clk, 1e9/sata_clk_freq)
        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk,
            phy.crg.cd_sata_tx.clk,
            phy.crg.cd_sata_rx.clk,
        )

    # Add PCIe -------------------------------------------------------------------------------------
    def add_pcie(self, name="pcie", phy=None, ndmas=0, max_pending_requests=8, address_width=32, data_width=None,
        with_dma_buffering    = True, dma_buffering_depth=1024,
        with_dma_loopback     = True,
        with_dma_synchronizer = False,
        with_dma_monitor      = False,
        with_dma_status       = False, status_width=32,
        with_dma_table        = True,
        with_msi              = True, msi_type="msi", msi_width=32, msis={},
        with_ptm              = False,
):
        # Imports
        from litepcie.phy.uspciephy import USPCIEPHY
        from litepcie.phy.usppciephy import USPPCIEPHY
        from litepcie.core import LitePCIeEndpoint, LitePCIeMSI, LitePCIeMSIMultiVector, LitePCIeMSIX
        from litepcie.frontend.dma import LitePCIeDMA
        from litepcie.frontend.wishbone import LitePCIeWishboneMaster

        # Checks.
        assert self.csr.data_width == 32

        # Endpoint.
        self.check_if_exists(f"{name}_endpoint")
        endpoint = LitePCIeEndpoint(phy,
            max_pending_requests = max_pending_requests,
            endianness           = phy.endianness,
            address_width        = address_width,
            with_ptm             = with_ptm,
        )
        self.add_module(name=f"{name}_endpoint", module=endpoint)

        # MMAP.
        self.check_if_exists(f"{name}_mmap")
        mmap = LitePCIeWishboneMaster(self.pcie_endpoint, base_address=self.mem_map["csr"])
        self.add_module(name=f"{name}_mmap", module=mmap)
        self.bus.add_master(name=f"{name}_mmap", master=mmap.wishbone)

        # MSI.
        if with_msi:
            assert msi_type in ["msi", "msi-multi-vector", "msi-x"]
            self.check_if_exists(f"{name}_msi")
            if msi_type == "msi":
                msi = LitePCIeMSI(width=msi_width)
            if msi_type == "msi-multi-vector":
                msi = LitePCIeMSIMultiVector(width=msi_width)
            if msi_type == "msi-x":
                msi = LitePCIeMSIX(endpoint=self.pcie_endpoint, width=msi_width)
            self.add_module(name=f"{name}_msi", module=msi)
            if msi_type in ["msi", "msi-multi-vector"]:
                self.comb += msi.source.connect(phy.msi)
            self.msis = msis

        # DMAs.
        for i in range(ndmas):
            assert with_msi
            self.check_if_exists(f"{name}_dma{i}")
            dma = LitePCIeDMA(phy, endpoint,
                with_buffering    = with_dma_buffering, buffering_depth=dma_buffering_depth,
                with_loopback     = with_dma_loopback,
                with_synchronizer = with_dma_synchronizer,
                with_monitor      = with_dma_monitor,
                with_status       = with_dma_status, status_width=status_width,
                with_table        = with_dma_table,
                address_width     = address_width,
                data_width        = data_width,
            )
            self.add_module(name=f"{name}_dma{i}", module=dma)
            if with_dma_table:
                self.msis[f"{name.upper()}_DMA{i}_WRITER"] = dma.writer.irq
                self.msis[f"{name.upper()}_DMA{i}_READER"] = dma.reader.irq
        self.add_constant("DMA_CHANNELS",   ndmas)
        self.add_constant("DMA_ADDR_WIDTH", address_width)

        # Map/Connect MSI IRQs.
        if with_msi:
            for i, (k, v) in enumerate(sorted(self.msis.items())):
                self.comb += msi.irqs[i].eq(v)
                self.add_constant(k + "_INTERRUPT", i)

        # Timing constraints.
        self.platform.add_false_path_constraints(self.crg.cd_sys.clk, phy.cd_pcie.clk)

    # Add Video ColorBars Pattern ------------------------------------------------------------------
    def add_video_colorbars(self, name="video_colorbars", phy=None, timings="800x600@60Hz", clock_domain="sys"):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, ColorBarsPattern

        # Video Timing Generator.
        self.check_if_exists(f"{name}_vtg")
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        self.add_module(name=f"{name}_vtg", module=vtg)

        # ColorsBars Pattern.
        self.check_if_exists(name)
        colorbars = ClockDomainsRenamer(clock_domain)(ColorBarsPattern())
        self.add_module(name=name, module=colorbars)

        # Connect Video Timing Generator to ColorsBars Pattern.
        self.comb += [
            vtg.source.connect(colorbars.vtg_sink),
            colorbars.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)
        ]

    # Add Video Terminal ---------------------------------------------------------------------------
    def add_video_terminal(self, name="video_terminal", phy=None, timings="800x600@60Hz", clock_domain="sys"):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, VideoTerminal

        # Video Timing Generator.
        self.check_if_exists(f"{name}_vtg")
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        self.add_module(name=f"{name}_vtg", module=vtg)

        # Video Terminal.
        timings = timings if isinstance(timings, str) else timings[0]
        vt = VideoTerminal(
            hres = int(timings.split("@")[0].split("x")[0]),
            vres = int(timings.split("@")[0].split("x")[1]),
        )
        vt = ClockDomainsRenamer(clock_domain)(vt)
        self.add_module(name=name, module=vt)

        # Connect Video Timing Generator to Video Terminal.
        self.comb += vtg.source.connect(vt.vtg_sink)

        # Connect UART to Video Terminal.
        uart_cdc = stream.ClockDomainCrossing([("data", 8)], cd_from="sys", cd_to=clock_domain)
        self.add_module(name=f"{name}_uart_cdc", module=uart_cdc)
        self.comb += [
            uart_cdc.sink.valid.eq(self.uart.tx_fifo.source.valid & self.uart.tx_fifo.source.ready),
            uart_cdc.sink.data.eq(self.uart.tx_fifo.source.data),
            uart_cdc.source.connect(vt.uart_sink),
        ]

        # Connect Video Terminal to Video PHY.
        self.comb += vt.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)

    # Add Video Framebuffer ------------------------------------------------------------------------
    def add_video_framebuffer(self, name="video_framebuffer", phy=None, timings="800x600@60Hz", clock_domain="sys", format="rgb888", fifo_depth=64*KILOBYTE):
        # Imports.
        from litex.soc.cores.video import VideoTimingGenerator, VideoFrameBuffer

        # Video Timing Generator.
        vtg = VideoTimingGenerator(default_video_timings=timings if isinstance(timings, str) else timings[1])
        vtg = ClockDomainsRenamer(clock_domain)(vtg)
        self.add_module(name=f"{name}_vtg", module=vtg)

        # Video FrameBuffer.
        timings = timings if isinstance(timings, str) else timings[0]
        base = self.mem_map.get(name, None)
        if base is None:
            self.bus.add_region(name, SoCRegion(
                origin = 0x40c00000,
                size   = 0x800000,
                linker = True)
            )
            base = self.bus.regions[name].origin
        hres = int(timings.split("@")[0].split("x")[0])
        vres = int(timings.split("@")[0].split("x")[1])
        vfb = VideoFrameBuffer(self.sdram.crossbar.get_port(),
            hres                  = hres,
            vres                  = vres,
            base                  = base,
            fifo_depth            = fifo_depth,
            format                = format,
            clock_domain          = clock_domain,
            clock_faster_than_sys = vtg.video_timings["pix_clk"] >= self.sys_clk_freq,
        )
        self.add_module(name=name, module=vfb)

        # Connect Video Timing Generator to Video FrameBuffer.
        self.comb += vtg.source.connect(vfb.vtg_sink)

        # Connect Video FrameBuffer to Video PHY.
        self.comb += vfb.source.connect(phy if isinstance(phy, stream.Endpoint) else phy.sink)

        # Constants.
        self.add_constant("VIDEO_FRAMEBUFFER_BASE", base)
        self.add_constant("VIDEO_FRAMEBUFFER_HRES", hres)
        self.add_constant("VIDEO_FRAMEBUFFER_VRES", vres)
        self.add_constant("VIDEO_FRAMEBUFFER_DEPTH", vfb.depth)

# LiteXSoCArgumentParser ---------------------------------------------------------------------------

from litex.build.parser import LiteXArgumentParser

class LiteXSoCArgumentParser(LiteXArgumentParser): pass # FIXME: Add compat and remove.
