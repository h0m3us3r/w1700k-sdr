#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A stand-in for `w1700k serve`: the controller protocol over stdin/stdout, no hardware.

Every request is appended to --log as one JSON line (the first line records this process's
pid), so a test can check what the spectrum server asked for. With --tuning-file, what the
"radio" does (centre, rate, running, packing) is written there before each state event, so a
fake exporter can follow it the way the real stream follows the radio. Behaviour:
  configure  busy "configuring", three progress events, then the new radio and stream state;
             a running stream is stopped first and started again afterwards
  start/stop the stream's running flag, and its packing: half above 160 Msps, as w1700k picks it
  load       the radio is loaded (it is from the start, unless --unloaded)
  tune       the LO and the stream's centre frequency
  gain       AGC, or LNA and LPF steps
  filter     the analog filter, 20 to 320 MHz
"""
import argparse
import copy
import json
import os
import sys
import time

LO_RANGE = {0: [2080000000, 2740000000], 1: [4650000000, 7410000000], 2: [4650000000, 7410000000]}
RF_BAND = {0: "2g", 1: "5g", 2: "6g"}


DEFAULT_LO = {0: 2442000000, 1: 5180000000, 2: 6105000000}   # each band's LO until tuned


RATES = {(40000000, 40000000): (0, 1, 2), (80000000, 80000000): (0, 1, 2), (160000000, 160000000): (0, 1, 2),
         (320000000, 320000000): (0, 1, 2), (320000000, 640000000): (2,)}   # (sample, capture): bands


def packing_for(rate):
    return "half" if rate > 160000000 else "raw"


class Controller:
    def __init__(self, log_path, tuning_path, progress_delay, unloaded=False, running=False):
        self.log = open(log_path, "a", buffering=1)
        self.tuning_path = tuning_path
        self.delay = progress_delay
        self.log.write(json.dumps({"pid": os.getpid()}) + "\n")
        self.lo = dict(DEFAULT_LO)
        self.state = {
            "busy": None,
            "radio": {"loaded": True, "band": 1, "rf_band": "5g", "lo_hz": self.lo[1], "sample_rate": 40000000,
                      "capture_rate": 40000000,
                      "bands_ready": [0, 1, 2]},
            "stream": {"running": False, "packing": None, "sample_rate": 40000000, "center_hz": self.lo[1]},
            "rf": {"lo_hz": self.lo[1], "agc": True, "lna": None, "lpf": None, "filter_mhz": 20,
                   "lo_range_hz": LO_RANGE[1]},
            "error": None,
        }
        if unloaded:
            self.state["radio"]["loaded"] = False
        if running:
            self.state["stream"].update(running=True, packing=packing_for(40000000))

    def emit(self, message):
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def publish(self):
        if self.tuning_path:
            stream = self.state["stream"]
            temporary = self.tuning_path + ".new"
            with open(temporary, "w") as f:
                json.dump({k: stream[k] for k in ("center_hz", "sample_rate", "running", "packing")}, f)
            os.replace(temporary, self.tuning_path)
        self.emit({"event": "state", "state": copy.deepcopy(self.state)})

    def handle(self, request):
        op = request.get("op")
        handler = getattr(self, "op_" + str(op), None)
        if handler is None:
            raise ValueError("unknown op %r" % op)
        return handler(request)

    def op_status(self, request):
        return {"state": copy.deepcopy(self.state)}

    def op_load(self, request):
        self.state["radio"]["loaded"] = True
        self.publish()
        return {}

    def op_start(self, request):
        self.state["stream"]["running"] = True
        self.state["stream"]["packing"] = packing_for(self.state["stream"]["sample_rate"])
        self.publish()
        return {}

    def op_stop(self, request):
        self.state["stream"]["running"] = False
        self.publish()
        return {}

    def op_tune(self, request):
        hz = int(request["hz"])
        low, high = self.state["rf"]["lo_range_hz"]
        if not low <= hz <= high:
            raise ValueError("LO %d Hz is out of range" % hz)
        self.lo[self.state["radio"]["band"]] = hz
        self.state["rf"]["lo_hz"] = self.state["radio"]["lo_hz"] = hz
        self.state["stream"]["center_hz"] = hz
        self.publish()
        return {}

    def op_gain(self, request):
        rf = self.state["rf"]
        if request.get("agc"):
            rf.update(agc=True, lna=None, lpf=None)
        else:
            rf.update(agc=False, lna=int(request["lna"]), lpf=int(request["lpf"]))
        self.publish()
        return {}

    def op_filter(self, request):
        mhz = int(request["mhz"])
        if mhz not in (20, 40, 80, 160, 320):
            raise ValueError("filter: one of 20, 40, 80, 160, 320 MHz")
        self.state["rf"]["filter_mhz"] = mhz
        self.state["rf"]["filter_follows_rate"] = False
        self.publish()
        return {}

    def op_filter_follow(self, request):
        if not isinstance(request.get("on"), bool):
            raise ValueError("filter_follow: on must be true or false")
        rf = self.state["rf"]
        rf["filter_follows_rate"] = request["on"]
        if request["on"]:
            rf["filter_mhz"] = min(320, int(self.state["stream"]["sample_rate"]) // 1000000)
        self.publish()
        return {}

    def op_configure(self, request):
        band, rate = int(request["band"]), int(request["sample_rate"])
        capture = int(request.get("capture_rate") or rate)
        if band not in RATES.get((rate, capture), ()):
            raise ValueError("sample rate: 40, 80, 160 or 320 Msps; 640/2 on band 2")
        if request.get("lo_hz") is not None:
            self.lo[band] = int(request["lo_hz"])
        was_running = self.state["stream"]["running"]
        self.state["busy"] = "configuring"
        self.state["stream"]["running"] = False
        self.publish()
        for step, text in enumerate(["stopping the stream", "loading firmware", "initialising the radio"]):
            self.emit({"event": "progress", "text": text, "fraction": step / 3})
            time.sleep(self.delay)
        lo = self.lo[band]
        self.state["radio"].update(loaded=True, band=band, rf_band=RF_BAND[band], lo_hz=lo, sample_rate=rate,
                                   capture_rate=capture)
        self.state["stream"].update(sample_rate=rate, center_hz=lo, running=was_running,
                                    packing=packing_for(rate) if was_running else None)
        self.state["rf"].update(lo_hz=lo, lo_range_hz=LO_RANGE[band])   # the filter stays
        self.state["busy"] = None
        self.publish()
        return {}

    def serve(self):
        for line in sys.stdin:
            if not line.strip():
                continue
            request = json.loads(line)
            self.log.write(json.dumps(request) + "\n")
            reply = {"id": request.get("id")}
            try:
                reply.update(self.handle(request))
                reply["ok"] = True
            except (ValueError, KeyError) as e:
                reply.update(ok=False, error=str(e))
            self.emit(reply)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", required=True, help="append every request here, one JSON line each")
    parser.add_argument("--tuning-file", help="write the stream's tuning here on every change")
    parser.add_argument("--progress-delay", type=float, default=0.2, help="seconds between configure steps")
    parser.add_argument("--unloaded", action="store_true", help="the radio is not loaded until a load")
    parser.add_argument("--running", action="store_true", help="the stream is already running")
    args = parser.parse_args()
    controller = Controller(args.log, args.tuning_file, args.progress_delay, args.unloaded, args.running)
    controller.publish()
    controller.serve()


if __name__ == "__main__":
    main()
