"""Smallest useful dummy beamline: 4 PVs and a fake scan.

Run:     python mini_ioc.py --list-pvs
Poke it: caproto-caget DUMMY:SCAN:NUMBER
         caproto-camonitor DUMMY:SCAN:BUSY
         caproto-caput DUMMY:SCAN:START 1
"""

import asyncio

from caproto.server import PVGroup, pvproperty, ioc_arg_parser, run

class DummyBeamline(PVGroup):
    busy = pvproperty(name="SCAN:BUSY", value=0, read_only=True,
                      doc="1 while a scan is running")
    number = pvproperty(name="SCAN:NUMBER", value=0, read_only=True)
    probe_size = pvproperty(name="PROBE:SIZE", value=0.3, units="um")
    start = pvproperty(name="SCAN:START", value=0,
                       doc="write 1 to start a scan")
    duration = pvproperty(name="SCAN:DURATION", value=5.0, units="s")
    
    @start.putter
    async def start(self, instance, value):
        if value == 1 and self.busy.value == 0:
            asyncio.create_task(self._run_scan())
        return 0
    
    async def _run_scan(self):
        await self.number.write(self.number.value + 1)
        await self.busy.write(1)
        print("scan %d started" % self.number.value, flush=True)
        await asyncio.sleep(self.duration.value)
        await self.busy.write(0)
        print("scan %d done" % self.number.value, flush=True)

if __name__ == "__main__":
    ioc_options, run_options = ioc_arg_parser(
        default_prefix="DUMMY:", desc="PtyGuy dummy beamline")
    run(DummyBeamline(**ioc_options).pvdb, **run_options)