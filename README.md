# bms-tester

Automatický test ochranného obvodu Li-ion článku (BMS / protection IC) – podpětí (UV),
přepětí (OV) a nadproudu (OCP) – s laboratorním zdrojem **Rigol DP832** přes USB.
Článek není potřeba: emuluje ho jeden kanál zdroje. Výsledkem je **PDF report v angličtině**
(souhrn PASS/FAIL, podrobnosti, grafy) a CSV se všemi vzorky.

Volitelně **MLAB USBI2C** (CP2112) spíná MOSFETem zátěžový odpor a ukazuje stav na dvou LED.

| Soubor | Účel |
|---|---|
| [`bms_test.py`](bms_test.py) | test ochrany (pass, UV, OV, OCP vybíjení/nabíjení) + report |
| [`switch_test.py`](switch_test.py) | zkouška spínání zátěžového odporu MOSFETem a LED |
| [`dp832.py`](dp832.py) | ovladač a CLI pro DP832 (USBTMC, bez pyvisa) |
| [`cp2112_gpio.py`](cp2112_gpio.py) | GPIO na MLAB USBI2C (CP2112) přes hidapi |
| [`loop.py`](loop.py) | jednoduchá smyčka přepínání napětí a výstupů |

## Instalace

Linux, Python 3. DP832 se připojuje přes kernel driver `usbtmc` (`/dev/usbtmcN`).

```bash
pip install -r requirements.txt
```

`/dev/usbtmc0` patří skupině `plugdev`. Pokud skript hlásí `Permission denied`,
přidej uživatele do skupiny `plugdev`, nebo vytvoř udev pravidlo
`/etc/udev/rules.d/99-rigol.rules`:

```
SUBSYSTEM=="usbmisc", ATTRS{idVendor}=="1ab1", ATTRS{idProduct}=="0e11", MODE="0666"
```

## Test ochrany Li-ion článku – `bms_test.py`

Zapojení: **CH1 = článek** (B+/B−), **CH2 = nabíječka** (P+/P−). Kanály jde změnit
`--cell-ch` / `--chg-ch`. **CH1− a CH2− se nesmí propojit** – většina ochran spíná
v záporné větvi a propojka by spínací tranzistory obešla.

```
   DP832 CH1 (článek)          ochrana            DP832 CH2 (nabíječka)
   CH1 + ─────────── B+                    P+ ─────────── CH2 +
                           [ ochrana ]
   CH1 − ─────────── B−                    P− ─────────── CH2 −
```

```bash
python3 bms_test.py --dut "BMS rev B #3" --uv-limits 2.4,3.0 --ov-limits 4.25,4.35
```

Testy (výchozí `--tests pass,uv,ov,ocp_dis,ocp_chg`, s `--load-gpio` bez `ocp_chg`):

1. **pass** – článek na `--vmid` (3,7 V) s limitem 100 mA, nabíječka musí vidět jeho napětí.
   Pokud ne, skript krátce připojí nabíječku (probuzení/uvolnění ochrany).
2. **uv** – snižuje napětí článku po `--step` (20 mV) až do vypnutí výstupu, pak napětí
   zvyšuje a zjistí, kdy se ochrana sama uvolní (případně ji uvolní nabíječkou).
3. **ov** – článek a nabíječka stoupají spolu, nabíječka je o `--chg-delta` (100 mV) výš.
   Vypnutí nabíjení se pozná poklesem napětí článku z napětí nabíječky na nastavení CH1.
4. **ocp_dis / ocp_chg** – nadproudová ochrana při vybíjení a nabíjení (viz níže).

Jak se pozná stav ochrany: DP832 neumí proud odebírat, takže CH2 ve „snímacím režimu“
(0,1 V / 50 mA) funguje jako voltmetr na výstupu ochrany s malou vybíjecí zátěží.

`--dwell` (1,5 s) musí být delší než zpoždění detekce ochrany (např. DW01: OV ~1,2 s).

Výsledek je ve `reports/<čas>_<dut>/`: `report.pdf` (anglicky, souhrn PASS/FAIL + grafy),
`data.csv` a PNG grafy. Na konci jsou oba výstupy vypnuté (i po Ctrl+C / SIGTERM).

### Proudové ochrany (OCP) – `ocp_dis`, `ocp_chg`

DP832 neumí proud odebírat, výkon musí shodit odpor. Skript před každým OCP testem
vyzve k připojení odporu (Enter = připojeno, číslo = skutečná hodnota odporu v Ω,
`s` = test přeskočit), ověří ho měřením proudu a po testu vyzve k odpojení.

| Test | Odpor | Kam | Jak se mění proud |
|---|---|---|---|
| `ocp_dis` vybíjení | R_L (`--r-load`, 20 Ω pro ~200 mA) | mezi **P+ a P−** (paralelně k CH2) | CH1 v **CC**, proudový limit roste od `--ocp-dis-start` (50 mA) po 5 mA (`--ocp-dis-step`); vypnutí = výstup spadne pod polovinu napětí článku |
| `ocp_chg` nabíjení | R_C (`--r-cell`, pro 1 A ≤ 3,3 Ω / 6 W) | mezi **B+ a B−** (paralelně k CH1) | CH2 jako nabíječka v CC 100 mA → `--ocp-chg-stop` (1 A) po 20 mA; vypnutí = pokles proudu CH2 |

Vybíjecí OCP: na odporu platí U = I·R, takže napětí článku roste s proudem. Při malém
startovním proudu je napětí článku pod UV a ochrana může vypnout na UV – report to označí.
Start nad UV: `--ocp-dis-start 0` (CV na `--ocp-vmin`, automaticky nad změřeným uvolněním UV),
nebo startovní proud ≥ UV / R_L. Shora omezuje `--ocp-vmax` (4,2 V, automaticky pod
změřeným OV) a `--ocp-dis-max` (1 A). Volba R_L ≈ U_článku / očekávaný práh
(3,9 V / 0,2 A ≈ 20 Ω).

R_C pohltí nabíjecí proud jen do U_článku / R_C (3,7 V / 5 Ω = 740 mA); strop nabíjecího
proudu se podle zadaného odporu sám omezí na 90 % této hodnoty. Proudový limit CH1 se
nastaví automaticky podle odporu. R_L ani R_C nesmí zůstat připojené při
UV/OV testech – skript to hlídá klidovým proudem (`--idle-max`, 80 mA).

```bash
python3 bms_test.py --dut "BMS #1" --tests pass,uv,ov,ocp_dis,ocp_chg \
    --uv-limits 2.4,3.0 --ov-limits 4.25,4.35 --ocp-dis-limits 0.17,0.23 --ocp-chg-limits 0.17,0.23
```

Bez terminálu (`--no-prompt`) skript na přepojení nečeká – odpor musí být připojený
předem a pouštět se má jen jeden OCP test.

### Spínání R_L MOSFETem a LED (MLAB USBI2C / CP2112)

Zapojení přípravku: **GPIO3** – MOSFET, který zemní R_L (1 = odpor připojen),
zem modulu na **P−** (ne B−!), **GPIO1** – LED1, **GPIO2** – LED2.

```bash
python3 switch_test.py --r-load 15          # zkouška spínání + LED
python3 bms_test.py --dut "BMS #1" --load-gpio 3 --r-load 15
```

`switch_test.py` bliká LED, pak s CH2 na 1 V / 200 mA několikrát spíná MOSFET, kontroluje
zpětné čtení GPIO a změnu proudu a spočte odpor v cestě (R_L + MOSFET + přívody).

S `--load-gpio 3` skript při vybíjecím OCP odpor spíná sám (ochrana se nejdřív ověří bez zátěže)
a LED ukazují stav: **LED1** svítí během testu, **LED2** když je R_L připojen. Po skončení
zůstane svítit **LED1 = vše PASS**, nebo **LED2 = FAIL / přerušeno**. R_L se na konci vždy odpojí.
Nabíjecí OCP potřebuje R_C na straně článku, který MOSFET nespíná – s `--load-gpio` se proto
ve výchozí sadě nespouští.

## Ovládání DP832 – `dp832.py`

```bash
python3 dp832.py status              # stav všech kanálů (nastavení + měření)
python3 dp832.py set 1 5.0 0.1       # CH1: 5 V, proudový limit 0.1 A
python3 dp832.py on 1                # zapnout výstup CH1
python3 dp832.py off all             # vypnout všechny výstupy
python3 dp832.py ovp 1 6.0           # přepěťová ochrana CH1 na 6 V
python3 dp832.py ocp 1 0.2           # nadproudová ochrana CH1 na 0.2 A
python3 dp832.py protection          # stav OVP/OCP
python3 dp832.py monitor -i 0.5 -c 1,2 -o log.csv   # logování do CSV
python3 dp832.py raw "*IDN?"         # libovolný SCPI příkaz
```

Rozsahy: CH1 a CH2 0–32 V / 3.2 A, CH3 0–5.3 V / 3.2 A (včetně přerozsahu).
Skript hodnoty mimo rozsah odmítne.

```python
from dp832 import DP832

with DP832() as psu:
    psu.set(1, 5.0, 0.1)
    psu.output(1, True)
    v, i, p = psu.measure(1)
```

## Smyčka přepínání – `loop.py`

```bash
python3 loop.py -c 3 -v 1.8,3.3,5 -i 0.1 -n 10 --on 2 --off 1 -o loop.csv
```

Cyklicky nastavuje napětí a zapíná/vypíná výstup. Zapnutý kanál použije jen s `--force`,
na konci vrátí původní stav.

## Licence

[GPL-3.0](LICENSE)
