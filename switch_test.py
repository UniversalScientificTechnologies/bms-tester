#!/usr/bin/env python3
"""Zkouška spínání zátěžového odporu MOSFETem (USBI2C GPIO) a LED.

Zapojení: R_L mezi P+ a MOSFETem (GPIO3), zem modulu na P−, CH2 na P+/P−.
CH2 se pustí na nízké napětí (výchozí 1 V / 100 mA) a měří proud do P+/P−:
  MOSFET vypnut -> jen klidový proud (ochrana, vypnutý CH1)
  MOSFET zapnut -> navíc proud přes R_L  (≈ U / R_L)
Ze změny proudu se spočte odpor v cestě (R_L + MOSFET + přívody).
CH1 zůstává vypnutý. Na konci je MOSFET vypnutý a CH2 vypnutý.

Použití:  python3 DP832/switch_test.py [-n 5] [--r-load 15] [--load-gpio 3]
"""
import argparse
import sys
import time

from cp2112_gpio import Fixture
from dp832 import DP832


def main():
    ap = argparse.ArgumentParser(description="Zkouška spínání R_L a LED na USBI2C")
    ap.add_argument("-n", "--cycles", type=int, default=5, help="počet cyklů zap/vyp")
    ap.add_argument("--r-load", type=float, help="očekávaný odpor R_L [Ω] (pro kontrolu)")
    ap.add_argument("--load-gpio", type=int, default=3)
    ap.add_argument("--led1", type=int, default=1)
    ap.add_argument("--led2", type=int, default=2)
    ap.add_argument("--chg-ch", type=int, default=2)
    ap.add_argument("--cell-ch", type=int, default=1)
    ap.add_argument("--probe-v", type=float, default=1.0, help="napětí CH2 při zkoušce [V]")
    ap.add_argument("--probe-i", type=float, default=0.2, help="proudový limit CH2 [A]")
    ap.add_argument("--settle", type=float, default=0.8, help="čekání po přepnutí [s]")
    a = ap.parse_args()

    fx = Fixture(load_pin=a.load_gpio, led1=a.led1, led2=a.led2)
    ok_all = True
    try:
        print("LED test: LED1 svítí 1 s, pak LED2 svítí 1 s, pak obě.")
        fx.led1.on(); time.sleep(1); fx.led1.off()
        fx.led2.on(); time.sleep(1); fx.led2.off()
        fx.led1.on(); fx.led2.on(); time.sleep(1); fx.led1.off(); fx.led2.off()

        with DP832() as psu:
            psu.output(a.cell_ch, False)
            psu.set(a.chg_ch, a.probe_v, a.probe_i)
            psu.output(a.chg_ch, True)
            fx.led1.on()  # zkouška běží
            rows = []
            try:
                for k in range(1, a.cycles + 1):
                    for state in (False, True):
                        fx.load.set(state)
                        fx.led2.set(state)
                        time.sleep(a.settle)
                        v, i, _ = psu.measure(a.chg_ch)
                        rb = fx.load.state()
                        mode = psu.mode(a.chg_ch)
                        rows.append((k, state, rb, v, i, mode))
                        print(f"cyklus {k}: MOSFET {'ZAP' if state else 'vyp'} "
                              f"(GPIO čte {'1' if rb else '0'}): CH2 {v:.3f} V {i*1000:6.1f} mA {mode}")
                        if rb != state:
                            ok_all = False
                            print("   ! zpětné čtení GPIO nesouhlasí")
            finally:
                fx.load.off()
                fx.led2.off()
                psu.output(a.chg_ch, False)
                psu.set(a.chg_ch, 0.1, 0.05)

        off = [r for r in rows if not r[1]]
        on = [r for r in rows if r[1]]
        i_off = sum(r[4] for r in off) / len(off)
        i_on = sum(r[4] for r in on) / len(on)
        v_on = sum(r[3] for r in on) / len(on)
        delta = i_on - i_off
        print(f"\nPrůměr: vypnuto {i_off*1000:.1f} mA, zapnuto {i_on*1000:.1f} mA "
              f"(rozdíl {delta*1000:.1f} mA)")
        spread_on = max(r[4] for r in on) - min(r[4] for r in on)
        spread_off = max(r[4] for r in off) - min(r[4] for r in off)
        if delta < 0.01:
            ok_all = False
            print("FAIL: proud se při sepnutí MOSFETu nezměnil – odpor se nespíná "
                  "(zkontroluj GND modulu na P−, GPIO, MOSFET).")
        elif spread_on > 0.3 * delta or spread_off > 0.3 * delta:
            ok_all = False
            print("FAIL: spínání není opakovatelné (rozptyl proudu mezi cykly).")
        else:
            if any(r[5] == "CC" for r in on):
                print(f"Při sepnutí CH2 v CC (limit {a.probe_i*1000:.0f} mA) – odpor nelze přesně "
                      f"spočítat, je menší než {v_on/delta:.1f} Ω.")
            else:
                r_est = v_on / delta
                print(f"Odpor v cestě (R_L + MOSFET + přívody): {r_est:.2f} Ω")
                if a.r_load and abs(r_est - a.r_load) > 0.25 * a.r_load:
                    ok_all = False
                    print(f"FAIL: očekáváno {a.r_load:g} Ω")
            print("Spínání MOSFETu: " + ("OK" if ok_all else "FAIL"))
    finally:
        # výsledek na LED: LED1 = OK, LED2 = chyba
        fx.led1.set(ok_all)
        fx.led2.set(not ok_all)
        fx.close(leave_leds=True)
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
