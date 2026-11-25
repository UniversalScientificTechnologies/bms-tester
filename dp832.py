#!/usr/bin/env python3
"""Ovládání laboratorního zdroje Rigol DP832 přes USB (USBTMC).

Používá přímo linuxový kernel driver `usbtmc` (/dev/usbtmcN), takže nepotřebuje
pyvisa ani žádné další knihovny. Uživatel musí mít práva k /dev/usbtmcN
(typicky skupina plugdev nebo udev pravidlo, viz README.md).

Použití jako knihovna:

    from dp832 import DP832
    with DP832() as psu:              # najde DP832 automaticky
        psu.set(1, 5.0, 0.1)          # CH1: 5 V, limit 0.1 A
        psu.output(1, True)
        print(psu.measure(1))         # (V, A, W)

Použití z příkazové řádky:  python dp832.py --help
"""
import argparse
import csv
import glob
import os
import sys
import time
from datetime import datetime

# Nastavitelné rozsahy kanálů DP832 (V, A) – jmenovitě 30 V/3 A a 5 V/3 A,
# přístroj ale dovolí nastavit až do přerozsahu 32 V/3.2 A a 5.3 V/3.2 A
LIMITS = {1: (32.0, 3.2), 2: (32.0, 3.2), 3: (5.3, 3.2)}
CHANNELS = (1, 2, 3)


class DP832:
    def __init__(self, device=None):
        """device: cesta k /dev/usbtmcN; None = najít DP832 automaticky."""
        self.device = device or self.find()
        self.fd = os.open(self.device, os.O_RDWR)
        self.idn = self.query("*IDN?")
        if "DP8" not in self.idn:
            self.close()
            raise RuntimeError(f"{self.device} není DP832: {self.idn}")

    # ---------------------------------------------------------------- spojení
    @staticmethod
    def find():
        for dev in sorted(glob.glob("/dev/usbtmc*")):
            try:
                fd = os.open(dev, os.O_RDWR)
                try:
                    os.write(fd, b"*IDN?\n")
                    if b"DP8" in os.read(fd, 4096):
                        return dev
                finally:
                    os.close(fd)
            except OSError:
                continue
        raise RuntimeError("DP832 nenalezen (zkontroluj USB kabel a práva k /dev/usbtmc*)")

    def write(self, cmd):
        os.write(self.fd, (cmd + "\n").encode())

    def query(self, cmd):
        self.write(cmd)
        return os.read(self.fd, 4096).decode().strip()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- pomocné
    @staticmethod
    def _ch(ch):
        ch = int(ch)
        if ch not in CHANNELS:
            raise ValueError(f"Neplatný kanál {ch}, povoleno 1-3")
        return ch

    def check_error(self):
        """Vrátí chybu z fronty přístroje; vyhodí výjimku, pokud nějaká je."""
        err = self.query(":SYSTem:ERRor?")
        if not err.startswith("0,"):
            raise RuntimeError(f"DP832 chyba: {err}")
        return err

    # ---------------------------------------------------------------- nastavení
    def set(self, ch, voltage=None, current=None):
        """Nastaví napětí [V] a/nebo proudový limit [A] kanálu."""
        ch = self._ch(ch)
        vmax, imax = LIMITS[ch]
        if voltage is not None:
            if not 0 <= voltage <= vmax:
                raise ValueError(f"CH{ch}: napětí {voltage} V mimo rozsah 0–{vmax} V")
            self.write(f":SOURce{ch}:VOLTage {voltage:.3f}")
        if current is not None:
            if not 0 <= current <= imax:
                raise ValueError(f"CH{ch}: proud {current} A mimo rozsah 0–{imax} A")
            self.write(f":SOURce{ch}:CURRent {current:.4f}")
        self.check_error()

    def setpoint(self, ch):
        """Vrátí nastavené (V, A) kanálu."""
        ch = self._ch(ch)
        # "CH1:30V/3A,5.00,0.055"
        _, v, i = self.query(f":APPLy? CH{ch}").split(",")
        return float(v), float(i)

    def output(self, ch, state):
        """Zapne/vypne výstup kanálu."""
        ch = self._ch(ch)
        self.write(f":OUTPut:STATe CH{ch},{'ON' if state else 'OFF'}")
        self.check_error()

    def all_off(self):
        for ch in CHANNELS:
            self.output(ch, False)

    def is_on(self, ch):
        return self.query(f":OUTPut:STATe? CH{self._ch(ch)}") == "ON"

    def mode(self, ch):
        """CV / CC / UR (unregulated)."""
        return self.query(f":OUTPut:MODE? CH{self._ch(ch)}")

    # ---------------------------------------------------------------- ochrany
    def set_ovp(self, ch, voltage, enable=True):
        ch = self._ch(ch)
        self.write(f":OUTPut:OVP:VALue CH{ch},{voltage:.3f}")
        self.write(f":OUTPut:OVP CH{ch},{'ON' if enable else 'OFF'}")
        self.check_error()

    def set_ocp(self, ch, current, enable=True):
        ch = self._ch(ch)
        self.write(f":OUTPut:OCP:VALue CH{ch},{current:.4f}")
        self.write(f":OUTPut:OCP CH{ch},{'ON' if enable else 'OFF'}")
        self.check_error()

    def protection(self, ch):
        """Vrátí dict se stavem OVP/OCP (hodnota, zapnuto, vybavilo)."""
        ch = self._ch(ch)
        out = {}
        for p in ("OVP", "OCP"):
            out[p] = {
                "value": float(self.query(f":OUTPut:{p}:VALue? CH{ch}")),
                "enabled": self.query(f":OUTPut:{p}? CH{ch}") == "ON",
                # FW 00.01.14 přijímá jen krátký tvar ":ALAR?" (ALARm/QUEStion vrací -113)
                "tripped": self.query(f":OUTP:{p}:ALAR? CH{ch}") == "YES",
            }
        return out

    def clear_protection(self, ch):
        ch = self._ch(ch)
        self.write(f":OUTPut:OVP:CLEar CH{ch}")
        self.write(f":OUTPut:OCP:CLEar CH{ch}")

    # ---------------------------------------------------------------- měření
    def measure(self, ch):
        """Vrátí změřené (V, A, W) na výstupu kanálu."""
        v, i, p = self.query(f":MEASure:ALL? CH{self._ch(ch)}").split(",")
        return float(v), float(i), float(p)

    def status(self):
        rows = []
        for ch in CHANNELS:
            sv, si = self.setpoint(ch)
            mv, mi, mp = self.measure(ch)
            rows.append(dict(ch=ch, on=self.is_on(ch), mode=self.mode(ch),
                             set_v=sv, set_i=si, v=mv, i=mi, p=mp))
        return rows


# ==================================================================== CLI
def _print_status(psu):
    print(psu.idn)
    print(f"{'CH':<4}{'výstup':<8}{'mód':<5}{'nast. V':>9}{'nast. A':>9}"
          f"{'U [V]':>9}{'I [A]':>9}{'P [W]':>9}")
    for r in psu.status():
        print(f"CH{r['ch']:<2}{'ON' if r['on'] else 'off':<8}{r['mode']:<5}"
              f"{r['set_v']:>9.3f}{r['set_i']:>9.3f}{r['v']:>9.3f}{r['i']:>9.4f}{r['p']:>9.3f}")


def _channels(arg):
    return list(CHANNELS) if arg == "all" else [int(arg)]


def main():
    ap = argparse.ArgumentParser(description="Ovládání Rigol DP832 přes USB")
    ap.add_argument("-d", "--device", help="/dev/usbtmcN (výchozí: autodetekce)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="vypíše stav všech kanálů")

    p = sub.add_parser("set", help="nastaví napětí a proudový limit")
    p.add_argument("ch", type=int)
    p.add_argument("voltage", type=float)
    p.add_argument("current", type=float, nargs="?")

    for name in ("on", "off"):
        p = sub.add_parser(name, help=f"{'zapne' if name == 'on' else 'vypne'} výstup (číslo nebo 'all')")
        p.add_argument("ch")

    p = sub.add_parser("ovp", help="nastaví a zapne přepěťovou ochranu")
    p.add_argument("ch", type=int)
    p.add_argument("voltage", type=float)

    p = sub.add_parser("ocp", help="nastaví a zapne nadproudovou ochranu")
    p.add_argument("ch", type=int)
    p.add_argument("current", type=float)

    p = sub.add_parser("protection", help="stav OVP/OCP")
    p.add_argument("ch", default="all", nargs="?")

    p = sub.add_parser("monitor", help="průběžně měří (Ctrl+C ukončí)")
    p.add_argument("-i", "--interval", type=float, default=1.0, help="perioda [s]")
    p.add_argument("-c", "--channels", default="1,2,3", help="např. 1,3")
    p.add_argument("-o", "--csv", help="ukládat do CSV souboru")

    p = sub.add_parser("raw", help="pošle SCPI příkaz (s '?' vypíše odpověď)")
    p.add_argument("scpi")

    a = ap.parse_args()
    with DP832(a.device) as psu:
        if a.cmd == "status":
            _print_status(psu)
        elif a.cmd == "set":
            psu.set(a.ch, a.voltage, a.current)
            print(f"CH{a.ch}: nastaveno {psu.setpoint(a.ch)}")
        elif a.cmd in ("on", "off"):
            for ch in _channels(a.ch):
                psu.output(ch, a.cmd == "on")
            _print_status(psu)
        elif a.cmd == "ovp":
            psu.set_ovp(a.ch, a.voltage)
        elif a.cmd == "ocp":
            psu.set_ocp(a.ch, a.current)
        elif a.cmd == "protection":
            for ch in _channels(a.ch):
                print(f"CH{ch}", psu.protection(ch))
        elif a.cmd == "raw":
            if "?" in a.scpi:
                print(psu.query(a.scpi))
            else:
                psu.write(a.scpi)
                print(psu.query(":SYSTem:ERRor?"))
        elif a.cmd == "monitor":
            chs = [int(c) for c in a.channels.split(",")]
            f = open(a.csv, "a", newline="") if a.csv else None
            w = csv.writer(f) if f else None
            if w and f.tell() == 0:
                w.writerow(["time"] + [f"CH{c}_{q}" for c in chs for q in ("V", "A", "W")])
            try:
                while True:
                    t = datetime.now().isoformat(timespec="milliseconds")
                    vals = [x for c in chs for x in psu.measure(c)]
                    print(t, "  ".join(f"CH{c}: {vals[3*k]:6.3f} V {vals[3*k+1]:7.4f} A"
                                       for k, c in enumerate(chs)), flush=True)
                    if w:
                        w.writerow([t] + vals)
                        f.flush()
                    time.sleep(a.interval)
            except KeyboardInterrupt:
                pass
            finally:
                if f:
                    f.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, PermissionError) as e:
        sys.exit(f"Chyba: {e}")
