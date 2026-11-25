"""Ovládání GPIO na MLAB USBI2C (Silicon Labs CP2112) přes hidapi.

Zapojení na testovacím přípravku:
    GPIO3 – MOSFET, který zemní zátěžový odpor R_L (1 = odpor připojen)
    GPIO1 – LED1, GPIO2 – LED2
"""
import hid

VID, PID = 0x10C4, 0xEA90
REP_CONFIG, REP_GET_VALUES, REP_SET_VALUES = 0x02, 0x03, 0x04


class CP2112:
    def __init__(self, outputs=()):
        """outputs: piny, které se nastaví jako výstupy push-pull (ostatní beze změny)."""
        self.h = hid.device()
        try:
            self.h.open(VID, PID)
        except OSError as e:
            raise RuntimeError("USBI2C (CP2112) nenalezen nebo bez práv k /dev/hidraw*") from e
        mask = 0
        for p in outputs:
            if not 0 <= p <= 7:
                raise ValueError("GPIO musí být 0–7")
            mask |= 1 << p
        cfg = self.h.get_feature_report(REP_CONFIG, 5)
        self.h.send_feature_report([REP_CONFIG, cfg[1] | mask, cfg[2] | mask, cfg[3], cfg[4]])

    def write(self, pin, level):
        m = 1 << pin
        self.h.send_feature_report([REP_SET_VALUES, m if level else 0, m])

    def read(self, pin):
        return bool(self.h.get_feature_report(REP_GET_VALUES, 2)[1] & (1 << pin))

    def close(self):
        self.h.close()


class CP2112Gpio:
    """Jeden výstup (např. spínač odporu) s volitelnou polaritou."""

    def __init__(self, pin, active_high=True, dev=None):
        self.pin = pin
        self.active_high = active_high
        self.own = dev is None
        self.dev = dev or CP2112(outputs=[pin])

    def set(self, on):
        self.dev.write(self.pin, on if self.active_high else not on)

    def state(self):
        lvl = self.dev.read(self.pin)
        return lvl if self.active_high else not lvl

    def on(self):
        self.set(True)

    def off(self):
        self.set(False)

    def close(self):
        try:
            self.off()
        finally:
            if self.own:
                self.dev.close()


class Fixture:
    """Testovací přípravek: spínač R_L + dvě LED na jednom CP2112."""

    def __init__(self, load_pin=3, led1=1, led2=2, load_active_high=True, led_active_high=True):
        self.dev = CP2112(outputs=[load_pin, led1, led2])
        self.load = CP2112Gpio(load_pin, load_active_high, self.dev)
        self.led1 = CP2112Gpio(led1, led_active_high, self.dev)
        self.led2 = CP2112Gpio(led2, led_active_high, self.dev)
        self.load.off()

    def close(self, leave_leds=True):
        """Vždy odpojí R_L; LED nechá (výsledek testu), pokud leave_leds."""
        try:
            self.load.off()
            if not leave_leds:
                self.led1.off()
                self.led2.off()
        finally:
            self.dev.close()
