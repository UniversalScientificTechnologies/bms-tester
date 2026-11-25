#!/usr/bin/env python3
"""Smyčka, která na DP832 cyklicky mění napětí a zapíná/vypíná výstupy.

V každém cyklu projde pro každý zvolený kanál seznam napětí:
nastaví napětí a proudový limit, zapne výstup, počká, změří, vypne, počká.
Na konci (i při Ctrl+C nebo chybě) vrátí kanálům původní nastavení a stav výstupu.

Kanál, jehož výstup je při startu zapnutý (tedy nejspíš něco napájí),
skript odmítne použít, pokud nedostane --force.

Příklady:
    python loop.py                                  # CH3, 1–5 V, nekonečně
    python loop.py -c 3 -v 1.8,3.3,5 -i 0.1 -n 10 --on 2 --off 1
    python loop.py -c 1,3 -v 3.3,5 -o loop.csv --force
"""
import argparse
import csv
import sys
import time
from datetime import datetime

from dp832 import DP832, LIMITS


def main():
    ap = argparse.ArgumentParser(description="Cyklické přepínání parametrů DP832")
    ap.add_argument("-d", "--device", help="/dev/usbtmcN (výchozí: autodetekce)")
    ap.add_argument("-c", "--channels", default="3", help="kanály, např. 3 nebo 1,3 (výchozí 3)")
    ap.add_argument("-v", "--voltages", default="1,2,3,4,5", help="seznam napětí [V]")
    ap.add_argument("-i", "--current", type=float, default=0.1, help="proudový limit [A]")
    ap.add_argument("-n", "--cycles", type=int, default=0, help="počet cyklů (0 = nekonečně)")
    ap.add_argument("--on", type=float, default=1.0, help="doba zapnutí [s]")
    ap.add_argument("--off", type=float, default=0.5, help="doba vypnutí [s]")
    ap.add_argument("-o", "--csv", help="ukládat měření do CSV")
    ap.add_argument("--force", action="store_true",
                    help="povolit kanály, které jsou při startu zapnuté")
    a = ap.parse_args()

    chs = [int(c) for c in a.channels.split(",")]
    volts = [float(v) for v in a.voltages.split(",")]

    with DP832(a.device) as psu:
        # uložit původní stav
        orig = {ch: (psu.setpoint(ch), psu.is_on(ch)) for ch in chs}
        busy = [ch for ch, (_, on) in orig.items() if on]
        if busy and not a.force:
            sys.exit(f"Kanál(y) {busy} jsou zapnuté – nejspíš něco napájí. "
                     f"Použij --force, pokud je opravdu chceš přepínat.")
        # ověřit rozsahy dřív, než se cokoli změní
        for ch in chs:
            for v in volts:
                vmax, imax = LIMITS[ch]
                if not (0 <= v <= vmax and 0 <= a.current <= imax):
                    sys.exit(f"CH{ch}: {v} V / {a.current} A mimo rozsah {vmax} V / {imax} A")

        f = open(a.csv, "a", newline="") if a.csv else None
        w = csv.writer(f) if f else None
        if w and f.tell() == 0:
            w.writerow(["time", "cycle", "ch", "set_V", "set_A", "V", "A", "W", "mode"])

        cycle = 0
        try:
            while a.cycles == 0 or cycle < a.cycles:
                cycle += 1
                for v in volts:
                    for ch in chs:
                        psu.set(ch, v, a.current)
                        psu.output(ch, True)
                    time.sleep(a.on)
                    for ch in chs:
                        mv, mi, mp = psu.measure(ch)
                        mode = psu.mode(ch)
                        t = datetime.now().isoformat(timespec="milliseconds")
                        print(f"{t} cyklus {cycle:3d} CH{ch} nast. {v:6.3f} V -> "
                              f"{mv:6.3f} V {mi:7.4f} A {mode}", flush=True)
                        if w:
                            w.writerow([t, cycle, ch, v, a.current, mv, mi, mp, mode])
                    if f:
                        f.flush()
                    for ch in chs:
                        psu.output(ch, False)
                    time.sleep(a.off)
        except KeyboardInterrupt:
            print("\nPřerušeno.")
        finally:
            for ch, ((sv, si), on) in orig.items():
                psu.output(ch, False)
                psu.set(ch, sv, si)
                if on:
                    psu.output(ch, True)
            print("Obnoveno:", ", ".join(
                f"CH{ch} {sv} V/{si} A {'ON' if on else 'off'}"
                for ch, ((sv, si), on) in orig.items()))
            if f:
                f.close()


if __name__ == "__main__":
    main()
