#!/usr/bin/env python3
"""Test ochranného obvodu Li-ion článku (BMS / protection IC) pomocí DP832.

Zapojení (výchozí):
    CH1 = emulovaný článek  -> B+ / B-  ochrany
    CH2 = nabíječka         -> P+ / P-  ochrany (výstup)

Jak se pozná stav ochrany (DP832 umí proud jen dodávat, ne odebírat):
  * "Snímací režim" CH2: výstup zapnutý na 0,1 V / 50 mA. Zdroj neumí stáhnout
    cizí napětí, takže CH2 funguje jako voltmetr na P+/P- s malou vybíjecí
    zátěží (vnitřní bleeder). Vede-li ochrana ve směru vybíjení, CH2 měří ~U článku.
  * UV (podpětí) blokuje vybíjení -> napětí na CH2 spadne.
  * OV (přepětí) blokuje nabíjení. CH2 "nabíjí" napětím nad článkem; CH1 proud
    neodebere, takže uzel článku vystoupá na napětí nabíječky (emulace nabíjení).
    Po vypnutí nabíjecího tranzistoru napětí článku spadne zpět na nastavení CH1.

Testy:
  pass  – průchod: článek na středním napětí, CH2 musí vidět jeho napětí
  uv    – snižování napětí článku do vypnutí ochrany, pak zjištění uvolnění
  ov    – nabíjení s rostoucím napětím do vypnutí nabíjení, pak uvolnění

Výsledek: složka reports/<čas>_<dut>/ s report.md, data.csv a grafy (PNG).
Na konci jsou oba výstupy vypnuté.

Příklad:
    python bms_test.py --dut "BMS rev B #3" --uv-limits 2.4,3.0 --ov-limits 4.25,4.35
"""
import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime

from dp832 import DP832

HERE = os.path.dirname(os.path.abspath(__file__))


class Bench:
    """Obsluha dvou kanálů + logování všech vzorků."""

    def __init__(self, psu, a, writer):
        self.psu = psu
        self.a = a
        self.cell = a.cell_ch
        self.chg = a.chg_ch
        self.w = writer
        self.t0 = time.time()
        self.rows = []
        self.cell_set = None
        self.chg_set = None
        self.chg_iset = None
        self.chg_mode = None
        self.sw = None  # spínač zátěžového odporu R_L (CP2112 GPIO), None = ručně

    # -------------------------------------------------------------- ovládání
    def set_cell(self, v, i=None):
        self.psu.set(self.cell, v, self.a.cell_i if i is None else i)
        self.cell_set = v

    def sense_mode(self):
        """CH2 jako voltmetr s malou zátěží."""
        self.psu.set(self.chg, self.a.sense_v, self.a.sense_i)
        self.chg_set, self.chg_iset, self.chg_mode = self.a.sense_v, self.a.sense_i, "sense"

    def charge(self, v, i=None):
        i = self.a.chg_i if i is None else i
        self.psu.set(self.chg, v, i)
        self.chg_set, self.chg_iset, self.chg_mode = v, i, "charge"

    # -------------------------------------------------------------- měření
    def _measure(self, ch):
        # těsně po zapnutí výstupu vrací DP832 nesmysl (např. 429496.719 A) -> zopakovat
        for _ in range(5):
            v, i, p = self.psu.measure(ch)
            if abs(v) < 100 and abs(i) < 10:
                return v, i, p
            time.sleep(0.1)
        return v, i, p

    def sample(self, test, note=""):
        cv, ci, _ = self._measure(self.cell)
        pv, pi, _ = self._measure(self.chg)
        r = dict(t=round(time.time() - self.t0, 3), test=test,
                 cell_set=self.cell_set, cell_v=cv, cell_i=ci,
                 chg_mode=self.chg_mode, chg_set=self.chg_set, chg_iset=self.chg_iset,
                 chg_v=pv, chg_i=pi,
                 note=note)
        self.rows.append(r)
        self.w.writerow([r[k] for k in r])
        return r

    def dwell(self, test, check, note="", dwell=None):
        """Vzorkuje po dobu dwell; vrátí (první vzorek splňující check, nebo poslední, splněno?)."""
        end = time.time() + (self.a.dwell if dwell is None else dwell)
        r = self.sample(test, note)
        while True:
            if check(r):
                return r, True
            if time.time() >= end:
                return r, False
            time.sleep(self.a.sample_period)
            r = self.sample(test, note)

    # -------------------------------------------------------------- kritéria
    def discharge_on(self, r):
        """Ochrana vede ve směru vybíjení: CH2 vidí napětí článku."""
        return r["chg_v"] >= self.a.on_ratio * r["cell_v"] and r["cell_v"] > 0.5

    def discharge_off(self, r):
        return r["chg_v"] < self.a.off_ratio * r["cell_v"]

    def charge_follows(self, r):
        """Nabíjení prochází: uzel článku vytažen nabíječkou nad nastavení CH1."""
        lift = r["chg_set"] - r["cell_set"]
        return lift > 0 and (r["cell_v"] - r["cell_set"]) > 0.5 * lift

    def wake(self, test):
        """Připojení nabíječky (napětí = článek) – uvolní UV/power-down u většiny IO."""
        # vyšší proudový limit: na výstupu může být zátěž (R_L), kterou by nabíječka
        # se 100 mA neudržela na napětí článku a ochrana by se neuvolnila
        self.charge(self.cell_set, self.a.wake_i)
        time.sleep(self.a.wake_time)
        self.sample(test, "wake: nabíječka připojena")
        self.sense_mode()
        time.sleep(self.a.settle + 0.3)  # měření DP832 se obnovuje se zpožděním


# ======================================================================= testy
def test_pass(b, res):
    a = b.a
    b.set_cell(a.vmid)
    b.sense_mode()
    time.sleep(0.5)
    r, ok = b.dwell("pass", b.discharge_on)
    woke = False
    if not ok:
        b.wake("pass")
        r, ok = b.dwell("pass", b.discharge_on, "po wake")
        woke = True
    res["pass"] = dict(ok=ok, cell_v=r["cell_v"], chg_v=r["chg_v"], woke=woke)
    print(f"[pass] článek {r['cell_v']:.3f} V, nabíječka vidí {r['chg_v']:.3f} V "
          f"-> {'OK' if ok else 'NEVEDE'}{' (po wake)' if woke else ''}")
    return ok


def frange(start, stop, step):
    n = int(abs(stop - start) / step + 1e-6)  # nikdy nepřekročit stop
    sgn = 1 if stop >= start else -1
    return [round(start + sgn * k * step, 4) for k in range(n + 1)]


def test_uv(b, res):
    a = b.a
    out = res["uv"] = dict(trip=None, last_on=None, release=None, recovered=False)
    b.set_cell(a.vmid)
    b.sense_mode()
    r, ok = b.dwell("uv", b.discharge_on, "start")
    if not ok:
        b.wake("uv")
        r, ok = b.dwell("uv", b.discharge_on, "start po wake")
    if not ok:
        out["error"] = "Protection not conducting at start, UV test not possible."
        print("[uv]", out["error"])
        return

    # sestupná rampa
    for v in frange(a.vmid, a.uv_stop, a.step):
        b.set_cell(v)
        r, tripped = b.dwell("uv", b.discharge_off, "sweep down")
        if tripped:
            out["trip"] = r["cell_v"]
            out["trip_set"] = v
            print(f"[uv] VYPNUTO při článku {r['cell_v']:.3f} V (nastaveno {v:.3f} V)")
            break
        out["last_on"] = r["cell_v"]
        print(f"[uv] {v:.3f} V: vede (P {r['chg_v']:.3f} V)")
    else:
        print(f"[uv] ochrana nevypnula až do {a.uv_stop} V")
        b.set_cell(a.vmid)
        return

    # uvolnění: zvyšovat napětí bez nabíječky
    for v in frange(out["trip_set"], a.vmid, a.step):
        b.set_cell(v)
        r, on = b.dwell("uv", b.discharge_on, "release up")
        if on:
            out["release"] = r["cell_v"]
            print(f"[uv] uvolněno samo při {r['cell_v']:.3f} V")
            break
        print(f"[uv] {v:.3f} V: vypnuto (P {r['chg_v']:.3f} V)")
    else:
        print("[uv] samo se neuvolnilo do středního napětí – zkouším připojit nabíječku")
    b.set_cell(a.vmid)
    if out["release"] is None:
        b.wake("uv")
    r, out["recovered"] = b.dwell("uv", b.discharge_on, "obnovení")
    print(f"[uv] obnovení provozu: {'OK' if out['recovered'] else 'NE'}")


def test_ov(b, res):
    a = b.a
    out = res["ov"] = dict(trip=None, trip_chg_set=None, released=False)
    b.set_cell(a.vmid)
    b.sense_mode()
    r, ok = b.dwell("ov", b.discharge_on, "start")
    if not ok:
        b.wake("ov")

    # Článek i nabíječka stoupají spolu, nabíječka je o chg_delta výš a vytahuje
    # uzel článku nahoru. Po vypnutí nabíjení článek spadne jen na nastavení CH1
    # (o chg_delta níž) – zůstane nabitý jako skutečný článek, takže se ochrana
    # hned znovu neuvolní (při pádu až na vmid by oscilovala).
    last_follow = None
    for v in frange(a.vmid, a.ov_stop - a.chg_delta, a.step):
        b.set_cell(v)
        b.charge(round(v + a.chg_delta, 4))
        time.sleep(a.settle)
        # vzorkovat celou výdrž (zpoždění OV detekce); emulovaný článek po vypnutí
        # spadne na nastavení CH1 a ochrana se může hned znovu sepnout -> stačí
        # jediný vzorek bez nabíjení
        samples, end = [], time.time() + a.dwell
        while True:
            samples.append(b.sample("ov", "sweep up"))
            if time.time() >= end or not b.charge_follows(samples[-1]):
                break
            time.sleep(a.sample_period)
        r = samples[-1]
        if b.charge_follows(r):
            last_follow = max(samples, key=lambda s: s["cell_v"])
            print(f"[ov] nabíječka {r['chg_set']:.3f} V: článek {r['cell_v']:.3f} V, "
                  f"nabíjecí proud {r['chg_i']*1000:.1f} mA")
            continue
        follows = [s for s in samples if b.charge_follows(s)]
        if follows:
            last_follow = max(follows, key=lambda s: s["cell_v"])
        if last_follow is None:
            out["error"] = (f"No charge current path (charger {r['chg_set']:.3f} V, "
                            f"cell {r['cell_v']:.3f} V).")
            print("[ov]", out["error"])
            break
        out["trip"] = last_follow["cell_v"]
        out["trip_chg_set"] = r["chg_set"]
        out["after_v"] = r["cell_v"]
        print(f"[ov] NABÍJENÍ VYPNUTO – poslední napětí článku {last_follow['cell_v']:.3f} V, "
              f"po vypnutí {r['cell_v']:.3f} V")
        break
    else:
        print(f"[ov] ochrana nevypnula nabíjení až do {a.ov_stop} V")
        out["max_cell"] = last_follow["cell_v"] if last_follow else None

    # odpojit nabíječku, ověřit, že článek na středním napětí zase vede
    b.sense_mode()
    b.set_cell(a.vmid)
    r, out["released"] = b.dwell("ov", b.discharge_on, "po odpojení nabíječky")
    print(f"[ov] provoz po odpojení nabíječky: {'OK' if out['released'] else 'NE'}")


def prompt(a, msg):
    """Výzva k přepojení. Bez terminálu (--no-prompt) jen vypíše a pokračuje."""
    print(f"\n>>> {msg}")
    if not a.no_prompt:
        input("    Hotovo? Enter = pokračovat, Ctrl+C = ukončit test ")


def prompt_resistor(a, msg, r):
    """Výzva k připojení odporu. Vrátí skutečnou hodnotu odporu [Ω], nebo None = přeskočit."""
    print(f"\n>>> {msg}")
    if a.no_prompt:
        return r
    while True:
        ans = input(f"    Enter = připojeno {r:g} Ω, číslo = jiná hodnota [Ω], "
                    f"s = přeskočit test: ").strip().lower().replace(",", ".")
        if ans == "":
            return r
        if ans in ("s", "skip", "p"):
            return None
        try:
            val = float(ans)
            if val > 0:
                return val
        except ValueError:
            pass
        print("    Neplatná volba.")


def idle_ok(b, test):
    """Ověří, že na článku/výstupu není zapomenutý zátěžový odpor."""
    r = b.sample(test, "kontrola klidového proudu")
    if r["cell_i"] > b.a.idle_max:
        msg = (f"Idle cell current {r['cell_i']*1000:.0f} mA > {b.a.idle_max*1000:.0f} mA – "
               f"load resistor still connected? Test {test} skipped.")
        print(f"[{test}]", msg)
        return msg
    return None


def test_ocp_dis(b, res):
    """Vybíjecí OCP v režimu CC: odpor R_L mezi P+ a P−, CH2 zůstává jako voltmetr.
    CH1 (článek) jede v CC s napěťovým stropem ocp_vmax a jeho proudový limit se
    zvyšuje, dokud napětí na CH2 (výstup ochrany) nespadne na nulu.
    Proud přes ochranu = výstupní proud CH1 (včetně odběru snímacího CH2).
    Na odporu platí U = I·R, takže rozsah proudu je omezen napětím článku:
    zdola UV (ocp_vmin), shora ocp_vmax – po dosažení ocp_vmax přejde CH1 do CV."""
    a = b.a
    out = res["ocp_dis"] = dict(trip_i=None, last_i=None, last_v=None, recovered=False)
    # spodní napětí musí být nad uvolněním UV (pokud bylo v tomto běhu změřeno)
    vmin = a.ocp_vmin
    uv = res.get("uv") or {}
    uv_ok = uv.get("release") or uv.get("trip")
    if uv_ok and vmin < uv_ok + 0.05:
        vmin = round(uv_ok + 0.05, 3)
        print(f"[ocp_dis] spodní napětí zvýšeno na {vmin} V (UV ochrany {uv_ok:.3f} V)")
    out["vmin"] = vmin
    uv_level = uv_ok or a.ocp_vmin
    vceil = a.ocp_vmax
    ov_trip = (res.get("ov") or {}).get("trip")
    if ov_trip and vceil > ov_trip - 0.05:
        vceil = round(ov_trip - 0.05, 3)
        print(f"[ocp_dis] napěťový strop snížen na {vceil} V (OV ochrany {ov_trip:.3f} V)")
    out["vceil"] = vceil
    sw = b.sw
    b.sense_mode()
    if sw:
        # R_L spíná MOSFET: nejdřív ověřit, že ochrana bez zátěže vede
        sw.off()
        b.set_cell(a.vmid)
        time.sleep(a.settle + 0.3)  # čerstvé měření (DP832 se obnovuje se zpožděním)
        r, ok = b.dwell("ocp_dis", b.discharge_on, "bez zátěže")
        if not ok:
            b.wake("ocp_dis")
            r, ok = b.dwell("ocp_dis", b.discharge_on, "bez zátěže po wake")
        if not ok:
            out["error"] = "Protection not conducting even without load, discharge OCP not possible."
            print("[ocp_dis]", out["error"])
            return
        rl = a.r_load
        print(f"[ocp_dis] R_L = {rl:g} Ω spínán GPIO{a.load_gpio}; bez zátěže článek "
              f"{r['cell_v']:.3f} V, výstup {r['chg_v']:.3f} V, proud {r['cell_i']*1000:.1f} mA")
    else:
        b.set_cell(vmin, a.ocp_cell_i)
        rl = prompt_resistor(a, f"VYBÍJECÍ OCP: připoj odpor R_L = {a.r_load:g} Ω "
                                f"(≥ {a.ocp_vmax**2/a.r_load*1.5:.1f} W) mezi P+ a P− "
                                f"(paralelně k CH{a.chg_ch}). Proud půjde zhruba "
                                f"{(a.ocp_dis_start or vmin/a.r_load)*1000:.0f}–"
                                f"{a.ocp_vmax/a.r_load*1000:.0f} mA.",
                             a.r_load)
        if rl is None:
            out["skipped"] = True
            print("[ocp_dis] přeskočeno")
            b.set_cell(a.vmid)
            return
    a.r_load = rl
    cell_i = min(3.0, max(a.ocp_cell_i, 1.3 * a.ocp_vmax / rl))
    if a.ocp_dis_start:
        # start v CC od malého proudu; napětí článku bude zpočátku nízké (U = I·R)
        i0 = a.ocp_dis_start
        # bez zátěže by článek s CC limitem vyskočil na strop (nad OV) -> nejdřív
        # CV na vmid s proudovým limitem i0, připojit R_L (CH1 přejde do CC), pak strop
        b.psu.set(a.cell_ch, a.vmid, i0)
        if sw:
            sw.on()
        time.sleep(0.2)
        b.psu.set(a.cell_ch, vceil, i0)
        b.cell_set = vceil
        time.sleep(a.settle + 0.3)
        r = b.sample("ocp_dis", f"CC start {i0*1000:.0f} mA")
        if r["cell_v"] > 0.95 * vceil:
            if r["chg_v"] < a.off_ratio * r["cell_v"]:
                # výstup ochrany je vypnutý: při I·R pod UV ochrana hned odpojí
                out["error"] = (f"Protection cut off right at the start: {i0*1000:.0f} mA × "
                                f"{rl:g} Ω gives only ~{i0*rl:.2f} V on the cell, below UV "
                                f"({uv_level:.2f} V). Start current must be ≥ "
                                f"{uv_level/rl*1000:.0f} mA for R_L = {rl:g} Ω "
                                f"(--ocp-dis-start), or use --ocp-dis-start 0.")
            else:
                out["error"] = (f"R_L not detected: at {i0*1000:.0f} mA the cell is at "
                                f"{r['cell_v']:.3f} V (CV) – resistor not connected.")
    else:
        b.set_cell(vmin, cell_i)  # start v CV na spodním napětí
        if sw:
            sw.on()
        time.sleep(0.3)
        r, ok = b.dwell("ocp_dis", b.discharge_on, "start")
        if not ok:
            b.wake("ocp_dis")
            r, ok = b.dwell("ocp_dis", b.discharge_on, "start po wake")
        expected = r["cell_v"] / rl
        if not ok:
            out["error"] = "Protection not conducting at start, discharge OCP test not possible."
        elif r["cell_i"] < 0.7 * expected:
            out["error"] = (f"R_L not detected: cell current {r['cell_i']*1000:.0f} mA, "
                            f"expected ≥ {0.7*expected*1000:.0f} mA.")
        i0 = r["cell_i"]
    if "error" in out:
        print("[ocp_dis]", out["error"])
    else:
        out["start_i"] = i0
        print(f"[ocp_dis] start: článek {r['cell_v']:.3f} V, výstup {r['chg_v']:.3f} V, "
              f"proud {r['cell_i']*1000:.1f} mA")
        # vypnutí = výstup spadl pod polovinu napětí článku (funguje i při nízkém napětí)
        off = lambda r: r["chg_v"] < a.off_ratio * r["cell_v"] and r["cell_v"] - r["chg_v"] > 0.3
        i = round(i0, 3) - (a.ocp_dis_step if a.ocp_dis_start else 0)
        tripped = False
        while True:
            i = round(i + a.ocp_dis_step, 4)
            b.psu.set(a.cell_ch, vceil, i)  # CC: proudový limit, napěťový strop
            b.cell_set = vceil
            r, tripped = b.dwell("ocp_dis", off, f"CC {i*1000:.0f} mA", dwell=a.ocp_dwell)
            if tripped:
                out["trip_i"] = i
                out["trip_cell_v"] = out["last_v"] if out["last_v"] is not None else b.rows[-2]["cell_v"]
                print(f"[ocp_dis] VYPNUTO při proudu {i*1000:.0f} mA "
                      f"(článek {r['cell_v']:.3f} V, výstup {r['chg_v']:.3f} V, poslední vedoucí "
                      f"{fmt(out['last_i'] and out['last_i']*1000, 'mA')} "
                      f"při {fmt(out.get('last_v'))})")
                if out["last_v"] is None or out["last_v"] < uv_level:
                    out["maybe_uv"] = True
                    print(f"[ocp_dis] POZOR: článek byl pod UV ({uv_level:.2f} V) – "
                          f"vypnutí může být od UV, ne od OCP")
                break
            mode = b.psu.mode(a.cell_ch)
            print(f"[ocp_dis] CC {i*1000:.0f} mA: měřeno {r['cell_i']*1000:.1f} mA, "
                  f"článek {r['cell_v']:.3f} V, výstup {r['chg_v']:.3f} V {mode}")
            if i >= a.ocp_dis_max:
                print(f"[ocp_dis] dosažen strop {a.ocp_dis_max*1000:.0f} mA (--ocp-dis-max), "
                      f"nevypnulo")
                out["max_i"] = r["cell_i"]
                break
            if mode != "CC" or r["cell_i"] < i - 0.02:
                # napětí dosáhlo ocp_vmax -> víc proudu přes R_L nejde
                print(f"[ocp_dis] dosaženo {vceil} V – maximum pro R_L = {rl:g} Ω, "
                      f"nevypnulo do {r['cell_i']*1000:.0f} mA")
                out["max_i"] = r["cell_i"]
                break
            out["last_i"], out["last_v"] = r["cell_i"], r["cell_v"]
        b.set_cell(vmin, cell_i)

    manual_left = a.no_prompt and not sw  # odpor může zůstat připojený
    if sw:
        sw.off()
    else:
        prompt(a, "Odpoj odpor R_L z P+/P−.")
    # bez výzvy může odpor zůstat připojený -> ponechat vyšší proudový limit
    b.set_cell(a.vmid, cell_i if manual_left else None)
    time.sleep(a.settle + 0.3)  # měření DP832 se obnovuje se zpožděním
    rec = lambda r: b.discharge_on(r) and (manual_left or r["cell_i"] <= a.idle_max)
    r, ok = b.dwell("ocp_dis", rec, "po odpojení zátěže")
    if not ok:
        b.wake("ocp_dis")
        r, ok = b.dwell("ocp_dis", rec, "po odpojení zátěže + wake")
    out["recovered"] = ok
    print(f"[ocp_dis] obnovení po odpojení zátěže: {'OK' if out['recovered'] else 'NE'}")


def test_ocp_chg(b, res):
    """Nabíjecí OCP: odpor R_C paralelně k B+/B− pohlcuje nabíjecí proud (CH1 drží
    napětí článku), CH2 jako nabíječka v CC zvyšuje proud do vypnutí."""
    a = b.a
    out = res["ocp_chg"] = dict(trip_i=None, last_i=None, recovered=False,
                                stop=a.ocp_chg_stop)
    b.sense_mode()
    b.set_cell(a.vmid, a.ocp_cell_i)
    r_need = a.vmid * 0.9 / a.ocp_chg_stop
    rc = prompt_resistor(a, f"NABÍJECÍ OCP: připoj odpor R_C mezi B+ a B− (paralelně k CH{a.cell_ch}). "
                            f"Pro {a.ocp_chg_stop*1000:.0f} mA je potřeba ≤ {r_need:.1f} Ω "
                            f"(≥ {a.vmid**2/r_need*1.5:.0f} W); větší odpor omezí maximální proud.",
                         a.r_cell)
    if rc is None:
        out["skipped"] = True
        print("[ocp_chg] přeskočeno")
        b.set_cell(a.vmid)
        return
    a.r_cell = rc
    absorb = a.vmid / rc
    # R_C pohltí jen U/R; nad tím by nabíječka vytahovala napětí článku -> omezit strop
    stop = a.ocp_chg_stop
    if stop > 0.9 * absorb:
        stop = round(int(0.9 * absorb / a.ocp_chg_step) * a.ocp_chg_step, 4)
        print(f"[ocp_chg] R_C = {rc:g} Ω pohltí jen {absorb*1000:.0f} mA -> "
              f"strop proudu omezen na {stop*1000:.0f} mA")
    out["stop"] = stop
    cell_i = min(3.0, max(a.ocp_cell_i, 1.3 * absorb))
    b.set_cell(a.vmid, cell_i)
    time.sleep(0.3)
    r, ok = b.dwell("ocp_chg", b.discharge_on, "start")
    if not ok:
        b.wake("ocp_chg")
        r, ok = b.dwell("ocp_chg", b.discharge_on, "start po wake")
    expected = r["cell_v"] / a.r_cell
    if r["cell_i"] < 0.7 * expected:
        out["error"] = (f"R_C not detected: cell current {r['cell_i']*1000:.0f} mA, "
                        f"expected ≥ {0.7*expected*1000:.0f} mA.")
    elif not ok:
        out["error"] = "Protection not conducting at start, charge OCP test not possible."
    if "error" in out:
        print("[ocp_chg]", out["error"])
    else:
        vchg = round(a.vmid + a.ocp_chg_headroom, 3)
        for i in frange(a.ocp_chg_start, stop, a.ocp_chg_step):
            b.charge(vchg, i)
            time.sleep(a.settle)
            r, tripped = b.dwell("ocp_chg", lambda r, i=i: r["chg_i"] < 0.5 * i,
                                 "sweep up", dwell=a.ocp_dwell)
            if tripped and out["last_i"] is None:
                out["error"] = (f"No charge current flows (charger {r['chg_v']:.3f} V, "
                                f"{r['chg_i']*1000:.0f} mA) – check P+/P− wiring and fuse.")
                print("[ocp_chg]", out["error"])
                break
            if tripped:
                out["trip_i"] = i
                print(f"[ocp_chg] NABÍJENÍ VYPNUTO při nastavených {i*1000:.0f} mA "
                      f"(poslední tekoucí {fmt(out['last_i'] and out['last_i']*1000, 'mA')})")
                break
            if r["cell_v"] > a.vmid + 0.1:
                out["error"] = (f"Cell voltage rose to {r['cell_v']:.3f} V – "
                                f"R_C cannot absorb the charge current.")
                print("[ocp_chg]", out["error"])
                break
            out["last_i"] = r["chg_i"]
            print(f"[ocp_chg] nabíjecí proud {r['chg_i']*1000:.1f} mA "
                  f"(nastaveno {i*1000:.0f} mA), článek {r['cell_v']:.3f} V")
        else:
            print(f"[ocp_chg] nevypnulo do {stop*1000:.0f} mA")

    b.sense_mode()  # odpojit nabíječku – uvolní nabíjecí OCP
    if not str(out.get("error", "")).startswith("R_C not detected"):
        prompt(a, "Odpoj odpor R_C z B+/B−.")
    # bez výzvy může odpor zůstat připojený -> ponechat vyšší proudový limit
    b.set_cell(a.vmid, cell_i if a.no_prompt else None)
    r, ok = b.dwell("ocp_chg", b.discharge_on, "po odpojení")
    out["recovered"] = ok and (a.no_prompt or r["cell_i"] <= a.idle_max)
    print(f"[ocp_chg] provoz po odpojení: {'OK' if out['recovered'] else 'NE'}")


# ======================================================================= report
def verdict(value, limits):
    if limits is None or value is None:
        return ""
    lo, hi = limits
    return "PASS" if lo <= value <= hi else "FAIL"


def mlim(limits):
    return f"{limits[0]*1000:.0f}–{limits[1]*1000:.0f} mA" if limits else "—"


def vlim(limits):
    return f"{limits[0]:.3f}–{limits[1]:.3f} V" if limits else "—"


def fmt(v, unit="V"):
    if v is None:
        return "—"
    return f"{v:.1f} {unit}" if unit == "mA" else f"{v:.3f} {unit}"


def ma(v):
    return fmt(v * 1000 if v is not None else None, "mA")


def summary_rows(a, res):
    """Řádky souhrnné tabulky (anglicky): (test, result, limits, verdict)."""
    R = []
    p = res.get("pass")
    if p:
        R.append((f"Pass-through at {a.vmid} V",
                  f"cell {fmt(p['cell_v'])}, output {fmt(p['chg_v'])}"
                  f"{' (after wake-up)' if p['woke'] else ''}",
                  "output ≈ cell", "PASS" if p["ok"] else "FAIL"))
    u = res.get("uv")
    if u:
        if u.get("trip") is not None:
            R.append(("UV cut-off", fmt(u["trip"]), vlim(a.uv_limits), verdict(u["trip"], a.uv_limits)))
            R.append(("UV release",
                      fmt(u["release"]) if u.get("release") else "no auto-release (charger needed)",
                      "—", ""))
            R.append(("Recovery after UV", "yes" if u["recovered"] else "NO", "—",
                      "PASS" if u["recovered"] else "FAIL"))
        else:
            R.append(("UV cut-off", u.get("error", f"no cut-off down to {a.uv_stop} V"),
                      vlim(a.uv_limits), "FAIL"))
    o = res.get("ov")
    if o:
        if o.get("trip") is not None:
            R.append(("OV charge cut-off", fmt(o["trip"]), vlim(a.ov_limits),
                      verdict(o["trip"], a.ov_limits)))
        else:
            R.append(("OV charge cut-off", o.get("error", f"no cut-off up to {a.ov_stop} V"),
                      vlim(a.ov_limits), "FAIL"))
        R.append(("Operation after charger removal", "yes" if o.get("released") else "NO", "—",
                  "PASS" if o.get("released") else "FAIL"))
    d = res.get("ocp_dis")
    if d and d.get("skipped"):
        R.append(("Discharge OCP", "skipped", "—", ""))
    elif d:
        if "error" in d:
            val, v = d["error"], "FAIL"
        elif d["trip_i"] is not None:
            val = f"{d['trip_i']*1000:.0f} mA (last conducting {ma(d['last_i'])})"
            v = verdict(d["trip_i"], a.ocp_dis_limits)
            if d.get("maybe_uv"):
                val += f"; cell below UV ({fmt(d.get('trip_cell_v'))}) – may be UV cut-off"
                v = v or "?"
        else:
            val = f"no cut-off up to {ma(d.get('max_i'))} ({d.get('vceil', a.ocp_vmax)} V limit on R_L)"
            v = "FAIL"
        R.append(("Discharge OCP", val, mlim(a.ocp_dis_limits), v))
        R.append(("Recovery after load removal", "yes" if d["recovered"] else "NO", "—",
                  "PASS" if d["recovered"] else "FAIL"))
    c = res.get("ocp_chg")
    if c and c.get("skipped"):
        R.append(("Charge OCP", "skipped", "—", ""))
    elif c:
        if "error" in c:
            val, v = c["error"], "FAIL"
        elif c["trip_i"] is not None:
            val = f"{c['trip_i']*1000:.0f} mA (last flowing {ma(c['last_i'])})"
            v = verdict(c["trip_i"], a.ocp_chg_limits)
        else:
            val, v = f"no cut-off up to {c['stop']*1000:.0f} mA", "FAIL"
        R.append(("Charge OCP", val, mlim(a.ocp_chg_limits), v))
        R.append(("Operation after charger removal (OCP)", "yes" if c["recovered"] else "NO", "—",
                  "PASS" if c["recovered"] else "FAIL"))
    if res.get("interrupted"):
        R.append(("TEST INTERRUPTED", "report incomplete", "", "FAIL"))
    return R


def detail_lines(a, res):
    D = []
    u, o = res.get("uv"), res.get("ov")
    d, c = res.get("ocp_dis"), res.get("ocp_chg")
    if u and u.get("trip") is not None:
        D.append(f"UV: cell voltage swept down from {a.vmid} V in {a.step*1000:.0f} mV steps "
                 f"({a.dwell} s per step). Last voltage with output on: {fmt(u.get('last_on'))}; "
                 f"cut-off at {fmt(u['trip'])}. Release found by sweeping the voltage back up.")
    if o:
        D.append(f"OV: cell (CH{a.cell_ch}) and charger (CH{a.chg_ch}) ramp up together, charger "
                 f"{a.chg_delta*1000:.0f} mV above the cell. A charge cut-off is detected when the "
                 f"cell voltage drops from the charger level back to the CH{a.cell_ch} setpoint.")
        if o.get("trip") is not None:
            D.append(f"OV: last cell voltage while charging {fmt(o['trip'])}; charger set to "
                     f"{o['trip_chg_set']} V; cell after cut-off {fmt(o.get('after_v'))}.")
    if d and not d.get("skipped"):
        start = (f"from {d['start_i']*1000:.0f} mA" if d.get("start_i") is not None else "")
        D.append(f"Discharge OCP: R_L = {a.r_load:g} Ω across P+/P−, CH{a.cell_ch} in constant-current "
                 f"mode, current limit increased {start} in {a.ocp_dis_step*1000:.0f} mA steps "
                 f"({a.ocp_dwell} s per step, voltage ceiling {d.get('vceil', a.ocp_vmax)} V). Cut-off = output "
                 f"(CH{a.chg_ch}) below {a.off_ratio:.0%} of cell voltage. The current through the "
                 f"protection equals the CH{a.cell_ch} output current (incl. the CH{a.chg_ch} sense load)."
                 + (f" R_L is switched by a MOSFET (USBI2C GPIO{a.load_gpio}); the protection is "
                    f"verified to conduct without load before R_L is connected."
                    if a.load_gpio is not None else ""))
    if c and not c.get("skipped"):
        D.append(f"Charge OCP: R_C = {a.r_cell:g} Ω across B+/B− absorbs the charge current; "
                 f"charger (CH{a.chg_ch}) in constant-current mode from {a.ocp_chg_start*1000:.0f} "
                 f"to {c['stop']*1000:.0f} mA in {a.ocp_chg_step*1000:.0f} mA steps "
                 f"(voltage {a.vmid + a.ocp_chg_headroom:.2f} V, {a.ocp_dwell} s per step). "
                 f"Cut-off = charge current drops below 50 % of the setpoint.")
    D.append("Voltages are measured by the power supply at its terminals; wiring drop is not "
             "compensated. The cell is emulated by the power supply (no real cell connected).")
    return D


def write_report(outdir, a, idn, res, rows, started, plots):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)
    from xml.sax.saxutils import escape

    font, bold = "Helvetica", "Helvetica-Bold"
    ttf = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if os.path.exists(ttf):  # kvůli znakům Ω, ≈, ≥
        pdfmetrics.registerFont(TTFont("DejaVu", ttf))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", ttf.replace(".ttf", "-Bold.ttf")))
        font, bold = "DejaVu", "DejaVu-Bold"
    ss = getSampleStyleSheet()
    st = {k: ParagraphStyle(k, parent=ss[p], fontName=f, **kw) for k, p, f, kw in (
        ("title", "Title", bold, {}),
        ("h2", "Heading2", bold, {}),
        ("body", "BodyText", font, {"fontSize": 9, "leading": 12}),
        ("cell", "BodyText", font, {"fontSize": 8.5, "leading": 10.5}),
        ("cellb", "BodyText", bold, {"fontSize": 8.5, "leading": 10.5}),
    )}
    P = lambda t, s="body": Paragraph(escape(str(t)), st[s])

    path = os.path.join(outdir, "report.pdf")
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"Battery protection test – {a.dut or 'DUT'}")
    E = [P(f"Battery protection circuit test – {a.dut or 'unnamed DUT'}", "title")]

    info = [
        ("Date", f"{started:%Y-%m-%d %H:%M:%S}"),
        ("Power supply", idn),
        ("Wiring", f"cell emulation = CH{a.cell_ch} (B+/B−), charger/output = CH{a.chg_ch} (P+/P−)"),
        ("Nominal cell voltage", f"{a.vmid} V, cell current limit {a.cell_i*1000:.0f} mA"),
        ("Tests", a.tests),
    ]
    t = Table([[P(k, "cellb"), P(v, "cell")] for k, v in info], colWidths=[40 * mm, 140 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    E += [t, Spacer(1, 4 * mm), P("Summary", "h2")]

    rows_s = summary_rows(a, res)
    data = [[P(h, "cellb") for h in ("Test", "Result", "Limits", "Verdict")]]
    data += [[P(x, "cell") for x in r[:3]] + [P(r[3], "cellb")] for r in rows_s]
    t = Table(data, colWidths=[50 * mm, 85 * mm, 28 * mm, 17 * mm], repeatRows=1)
    style = [("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6")),
             ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for k, r in enumerate(rows_s, start=1):
        col = {"PASS": "#c8f0c8", "FAIL": "#f5c2c2", "?": "#fbe7b5"}.get(r[3])
        if col:
            style.append(("BACKGROUND", (3, k), (3, k), colors.HexColor(col)))
    t.setStyle(TableStyle(style))
    E += [t, Spacer(1, 4 * mm), P("Details", "h2")]
    E += [P("• " + line) for line in detail_lines(a, res)]
    E += [Spacer(1, 2 * mm), P("Raw data: data.csv (same folder).")]

    for title, fn in plots:
        img = Image(os.path.join(outdir, fn), width=180 * mm, height=120 * mm)
        E.append(KeepTogether([P(title, "h2"), img]))
    doc.build(E)
    return path


def make_plots(outdir, rows):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib není k dispozici – grafy vynechány")
        return []
    plots = []
    for test, title in (("uv", "UV test"), ("ov", "OV test"),
                        ("ocp_dis", "Discharge OCP"), ("ocp_chg", "Charge OCP")):
        rs = [r for r in rows if r["test"] == test]
        if not rs:
            continue
        t = [r["t"] - rs[0]["t"] for r in rs]
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(9, 6))
        ax1.plot(t, [r["cell_v"] for r in rs], label="cell measured")
        ax1.plot(t, [r["cell_set"] for r in rs], "--", label="cell setpoint")
        ax1.plot(t, [r["chg_v"] for r in rs], label="charger / output")
        ax1.set_ylabel("U [V]")
        ax1.legend()
        ax1.grid(True)
        ax2.plot(t, [r["cell_i"] * 1000 for r in rs], label="cell")
        ax2.plot(t, [r["chg_i"] * 1000 for r in rs], label="charger")
        ax2.set_ylabel("I [mA]")
        ax2.set_xlabel("t [s]")
        ax2.legend()
        ax2.grid(True)
        fig.suptitle(title)
        fig.tight_layout()
        fn = f"{test}.png"
        fig.savefig(os.path.join(outdir, fn), dpi=130)
        plt.close(fig)
        plots.append((title, fn))
    return plots


# ======================================================================= main
def pair(s):
    lo, hi = (float(x) for x in s.split(","))
    return lo, hi


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Test ochrany Li-ion článku (UV/OV/OCP) s DP832")
    ap.add_argument("--dut", default="", help="označení testovaného kusu")
    ap.add_argument("-d", "--device")
    ap.add_argument("--cell-ch", type=int, default=1)
    ap.add_argument("--chg-ch", type=int, default=2)
    ap.add_argument("--tests", default=None,
                    help="výběr z pass,uv,ov,ocp_dis,ocp_chg; výchozí vše, s --load-gpio bez ocp_chg")
    ap.add_argument("--vmid", type=float, default=3.7, help="střední napětí článku [V]")
    ap.add_argument("--cell-i", type=float, default=0.1, help="proudový limit článku [A]")
    ap.add_argument("--chg-i", type=float, default=0.1, help="proudový limit nabíječky [A]")
    ap.add_argument("--uv-stop", type=float, default=2.3, help="nejnižší napětí UV rampy [V]")
    ap.add_argument("--ov-stop", type=float, default=4.5, help="nejvyšší napětí OV rampy [V]")
    ap.add_argument("--step", type=float, default=0.02, help="krok rampy [V]")
    ap.add_argument("--dwell", type=float, default=1.5,
                    help="výdrž na kroku [s] – musí být delší než zpoždění detekce ochrany")
    ap.add_argument("--sample-period", type=float, default=0.2)
    ap.add_argument("--chg-delta", type=float, default=0.1,
                    help="o kolik je nabíječka nad článkem při OV testu [V]")
    ap.add_argument("--settle", type=float, default=0.5,
                    help="ustálení po změně napětí nabíječky před vzorkováním [s]")
    ap.add_argument("--wake-time", type=float, default=1.0)
    ap.add_argument("--wake-i", type=float, default=0.5,
                    help="proudový limit nabíječky při probouzení ochrany [A]")
    ap.add_argument("--sense-v", type=float, default=0.1, help="napětí CH2 ve snímacím režimu")
    ap.add_argument("--sense-i", type=float, default=0.05)
    ap.add_argument("--on-ratio", type=float, default=0.9,
                    help="výstup vede, když U_výstup >= ratio * U_článek")
    ap.add_argument("--off-ratio", type=float, default=0.5,
                    help="výstup vypnut, když U_výstup < ratio * U_článek")
    ap.add_argument("--uv-limits", type=pair, help="povolený rozsah UV prahu, např. 2.4,3.0")
    ap.add_argument("--ov-limits", type=pair, help="povolený rozsah OV prahu, např. 4.25,4.35")
    g = ap.add_argument_group("OCP")
    g.add_argument("--r-load", type=float, default=20.0,
                   help="vybíjecí odpor R_L mezi P+/P− [Ω]")
    g.add_argument("--ocp-vmin", type=float, default=3.1,
                   help="napětí článku na začátku vybíjecí rampy [V] (musí být nad UV)")
    g.add_argument("--ocp-vmax", type=float, default=4.2,
                   help="napětí článku na konci vybíjecí rampy [V] (musí být pod OV)")
    g.add_argument("--ocp-dis-start", type=float, default=0.05,
                   help="počáteční proud CC rampy vybíjecího OCP [A]; 0 = začít v CV na --ocp-vmin")
    g.add_argument("--ocp-dis-max", type=float, default=1.0,
                   help="strop proudu vybíjecí rampy [A]")
    g.add_argument("--ocp-dis-step", type=float, default=0.005,
                   help="krok proudového limitu CH1 při vybíjecím OCP [A]")
    g.add_argument("--zero-v", type=float, default=0.5,
                   help="výstup ochrany považován za odpojený pod tímto napětím [V]")
    g.add_argument("--r-cell", type=float, default=10.0,
                   help="odpor R_C paralelně k článku pro nabíjecí OCP [Ω]")
    g.add_argument("--ocp-chg-start", type=float, default=0.1, help="počáteční nabíjecí proud [A]")
    g.add_argument("--ocp-chg-stop", type=float, default=1.0,
                   help="maximální nabíjecí proud [A] (omezí se podle R_C)")
    g.add_argument("--ocp-chg-step", type=float, default=0.02, help="krok nabíjecího proudu [A]")
    g.add_argument("--ocp-chg-headroom", type=float, default=0.4,
                   help="napětí nabíječky nad článkem při nabíjecím OCP [V]")
    g.add_argument("--ocp-dwell", type=float, default=0.5, help="výdrž na kroku OCP [s]")
    g.add_argument("--ocp-cell-i", type=float, default=0.5,
                   help="proudový limit článku při OCP testech [A]")
    g.add_argument("--idle-max", type=float, default=0.08,
                   help="max. klidový proud článku (jinak je připojen zapomenutý odpor) [A]")
    g.add_argument("--ocp-dis-limits", type=pair, help="povolený vybíjecí OCP práh [A], např. 0.17,0.23")
    g.add_argument("--ocp-chg-limits", type=pair, help="povolený nabíjecí OCP práh [A], např. 0.17,0.23")
    g.add_argument("--load-gpio", type=int,
                   help="R_L spínaný MOSFETem z GPIO modulu USBI2C (CP2112), např. 3")
    g.add_argument("--led1", type=int, default=1, help="GPIO LED1 (test běží / PASS)")
    g.add_argument("--led2", type=int, default=2, help="GPIO LED2 (R_L připojen / FAIL)")
    g.add_argument("--load-active-low", action="store_true",
                   help="GPIO = 0 připojí odpor (výchozí: 1 = připojen)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="nečekat na Enter u výzev k přepojení (odpory musí být připojené předem)")
    ap.add_argument("--out", default=os.path.join(HERE, "reports"), help="složka pro reporty")
    ap.add_argument("-y", "--yes", action="store_true", help="nečekat na potvrzení")
    a = ap.parse_args(argv)
    if a.tests is None:
        # MOSFET spíná jen R_L na straně P; nabíjecí OCP potřebuje ruční R_C na straně B
        a.tests = "pass,uv,ov,ocp_dis" if a.load_gpio is not None else "pass,uv,ov,ocp_dis,ocp_chg"
    if a.cell_ch == a.chg_ch:
        ap.error("článek a nabíječka musí být různé kanály")
    if not (a.uv_stop < a.vmid < a.ov_stop <= 4.6):
        ap.error("musí platit uv-stop < vmid < ov-stop <= 4.6 V")
    if not (a.uv_stop < a.ocp_vmin < a.ocp_vmax <= a.ov_stop):
        ap.error("musí platit uv-stop < ocp-vmin < ocp-vmax <= ov-stop")
    unknown = set(t.strip() for t in a.tests.split(",")) - {"pass", "uv", "ov", "ocp_dis", "ocp_chg"}
    if unknown:
        ap.error(f"neznámé testy: {', '.join(sorted(unknown))}")
    return a


class LoadSwitch:
    """Spínač R_L; LED2 svítí, když je odpor připojený."""

    def __init__(self, fx):
        self.fx = fx

    def on(self):
        self.fx.load.on()
        self.fx.led2.on()

    def off(self):
        self.fx.load.off()
        self.fx.led2.off()


def _sigterm(signum, frame):
    raise KeyboardInterrupt


def run(a, psu):
    # ukončení přes SIGTERM (kill, zavření terminálu) -> stejně jako Ctrl+C vypne výstupy
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGHUP, _sigterm)
    tests = [t.strip() for t in a.tests.split(",")]
    started = datetime.now()
    tag = "".join(c if c.isalnum() or c in "-_" else "_" for c in a.dut) or "dut"
    outdir = os.path.join(a.out, f"{started:%Y%m%d_%H%M%S}_{tag}")
    os.makedirs(outdir, exist_ok=True)

    f = open(os.path.join(outdir, "data.csv"), "w", newline="")
    w = csv.writer(f)
    w.writerow(["t", "test", "cell_set", "cell_v", "cell_i", "chg_mode", "chg_set",
                "chg_iset", "chg_v", "chg_i", "note"])
    b = Bench(psu, a, w)
    fx = None
    if a.load_gpio is not None:
        from cp2112_gpio import Fixture
        fx = Fixture(load_pin=a.load_gpio, led1=a.led1, led2=a.led2,
                     load_active_high=not a.load_active_low)
        fx.led2.off()
        fx.led1.on()  # LED1 = test běží
        b.sw = LoadSwitch(fx)
    res = {}
    try:
        psu.output(a.chg_ch, False)
        # jen OCP testy -> odpor může být připojen už od startu, limit 100 mA by nestačil
        only_ocp = all(t.startswith("ocp") for t in tests)
        b.set_cell(a.vmid, a.ocp_cell_i if only_ocp else None)
        b.sense_mode()
        psu.output(a.cell_ch, True)
        psu.output(a.chg_ch, True)
        if "pass" in tests:
            test_pass(b, res)
        if "uv" in tests:
            err = idle_ok(b, "uv")
            if err:
                res["uv"] = dict(trip=None, error=err)
            else:
                test_uv(b, res)
        if "ov" in tests:
            err = idle_ok(b, "ov")
            if err:
                res["ov"] = dict(trip=None, released=False, error=err)
            else:
                test_ov(b, res)
        if "ocp_dis" in tests:
            test_ocp_dis(b, res)
        if "ocp_chg" in tests:
            test_ocp_chg(b, res)
    except KeyboardInterrupt:
        print("\nPřerušeno – report bude neúplný.")
        res["interrupted"] = True
    finally:
        if fx:
            fx.load.off()  # odpojí R_L
        psu.output(a.chg_ch, False)
        psu.output(a.cell_ch, False)
        f.close()
        plots = make_plots(outdir, b.rows)
        path = write_report(outdir, a, psu.idn, res, b.rows, started, plots)
        if fx:
            # výsledek na LED: LED1 = vše PASS, LED2 = nějaký FAIL / přerušeno
            failed = res.get("interrupted") or any(r[3] == "FAIL" for r in summary_rows(a, res))
            fx.led1.set(not failed)
            fx.led2.set(failed)
            fx.close(leave_leds=True)
            print(f"LED: {'LED2 – FAIL' if failed else 'LED1 – PASS'}")
        print(f"\nVýstupy vypnuty. Report: {path}")
    return res, path


def main():
    a = parse_args()
    with DP832(a.device) as psu:
        print(psu.idn)
        for ch in (a.cell_ch, a.chg_ch):
            sv, si = psu.setpoint(ch)
            print(f"  CH{ch}: {'ON ' if psu.is_on(ch) else 'off'} {sv} V / {si} A")
        print(f"Test převezme CH{a.cell_ch} (článek) a CH{a.chg_ch} (nabíječka), "
              f"napětí {a.uv_stop}–{a.ov_stop} V; na konci budou oba výstupy vypnuté.")
        print(f"Testy: {a.tests}")
        if not a.yes and input("Pokračovat? [y/N] ").strip().lower() not in ("y", "a", "ano", "yes"):
            sys.exit("Zrušeno.")
        run(a, psu)


if __name__ == "__main__":
    main()
